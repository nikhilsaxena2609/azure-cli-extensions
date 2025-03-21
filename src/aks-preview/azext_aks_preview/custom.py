# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import datetime
import json
import os
import os.path
import platform
import re
import ssl
import sys
import threading
import time
import uuid
import webbrowser

from azure.cli.core.api import get_config_dir
from azure.cli.core.azclierror import (
    ArgumentUsageError,
    InvalidArgumentValueError,
)
from azure.cli.core.commands import LongRunningOperation
from azure.cli.core.commands.client_factory import get_subscription_id
from azure.cli.core.util import (
    in_cloud_console,
    sdk_no_wait,
    shell_safe_json_parse,
)
from azure.graphrbac.models import (
    ApplicationCreateParameters,
    KeyCredential,
    PasswordCredential,
    ServicePrincipalCreateParameters,
)
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
from knack.log import get_logger
from knack.prompting import prompt_y_n
from knack.util import CLIError
from msrestazure.azure_exceptions import CloudError
from six.moves.urllib.error import URLError
from six.moves.urllib.request import urlopen

from azure.cli.command_modules.acs.addonconfiguration import (
    ensure_container_insights_for_monitoring,
    sanitize_loganalytics_ws_resource_id,
    ensure_default_log_analytics_workspace_for_monitoring
)

from azext_aks_preview._client_factory import (
    CUSTOM_MGMT_AKS_PREVIEW,
    cf_agent_pools,
    get_container_registry_client,
    get_auth_management_client,
    get_graph_rbac_management_client,
    get_msi_client,
    get_resource_by_name,
)

from azext_aks_preview._consts import (
    ADDONS,
    ADDONS_DESCRIPTIONS,
    CONST_ACC_SGX_QUOTE_HELPER_ENABLED,
    CONST_AZURE_KEYVAULT_SECRETS_PROVIDER_ADDON_NAME,
    CONST_CONFCOM_ADDON_NAME,
    CONST_INGRESS_APPGW_ADDON_NAME,
    CONST_INGRESS_APPGW_APPLICATION_GATEWAY_ID,
    CONST_INGRESS_APPGW_APPLICATION_GATEWAY_NAME,
    CONST_INGRESS_APPGW_SUBNET_CIDR,
    CONST_INGRESS_APPGW_SUBNET_ID,
    CONST_INGRESS_APPGW_WATCH_NAMESPACE,
    CONST_KUBE_DASHBOARD_ADDON_NAME,
    CONST_MONITORING_ADDON_NAME,
    CONST_MONITORING_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID,
    CONST_MONITORING_USING_AAD_MSI_AUTH,
    CONST_NODEPOOL_MODE_USER,
    CONST_OPEN_SERVICE_MESH_ADDON_NAME,
    CONST_ROTATION_POLL_INTERVAL,
    CONST_SCALE_DOWN_MODE_DELETE,
    CONST_SCALE_SET_PRIORITY_REGULAR,
    CONST_SECRET_ROTATION_ENABLED,
    CONST_SPOT_EVICTION_POLICY_DELETE,
    CONST_VIRTUAL_NODE_ADDON_NAME,
    CONST_VIRTUAL_NODE_SUBNET_NAME,
)
from azext_aks_preview._helpers import print_or_merge_credentials, get_nodepool_snapshot_by_snapshot_id, get_cluster_snapshot_by_snapshot_id
from azext_aks_preview._podidentity import (
    _ensure_managed_identity_operator_permission,
    _ensure_pod_identity_addon_is_enabled,
    _fill_defaults_for_pod_identity_profile,
    _update_addon_pod_identity,
)
from azext_aks_preview._resourcegroup import get_rg_location
from azext_aks_preview._roleassignments import (
    add_role_assignment,
    build_role_scope,
    resolve_object_id,
    resolve_role_id,
)
from azext_aks_preview.addonconfiguration import (
    add_ingress_appgw_addon_role_assignment,
    add_monitoring_role_assignment,
    add_virtual_node_role_assignment,
    enable_addons
)
from azext_aks_preview.aks_draft.commands import (
    aks_draft_cmd_create,
    aks_draft_cmd_generate_workflow,
    aks_draft_cmd_setup_gh,
    aks_draft_cmd_up,
    aks_draft_cmd_update,
)
from azext_aks_preview.maintenanceconfiguration import (
    aks_maintenanceconfiguration_update_internal,
)
from azext_aks_preview.aks_diagnostics import (
    aks_kollect_cmd,
    aks_kanalyze_cmd,
)

logger = get_logger(__name__)


def wait_then_open(url):
    """
    Waits for a bit then opens a URL.  Useful for waiting for a proxy to come up, and then open the URL.
    """
    for _ in range(1, 10):
        try:
            urlopen(url, context=_ssl_context())
        except URLError:
            time.sleep(1)
        break
    webbrowser.open_new_tab(url)


def wait_then_open_async(url):
    """
    Spawns a thread that waits for a bit then opens a URL.
    """
    t = threading.Thread(target=wait_then_open, args=({url}))
    t.daemon = True
    t.start()


def _ssl_context():
    if sys.version_info < (3, 4) or (in_cloud_console() and platform.system() == 'Windows'):
        try:
            # added in python 2.7.13 and 3.6
            return ssl.SSLContext(ssl.PROTOCOL_TLS)
        except AttributeError:
            return ssl.SSLContext(ssl.PROTOCOL_TLSv1)

    return ssl.create_default_context()


def _delete_role_assignments(cli_ctx, role, service_principal, delay=2, scope=None):
    # AAD can have delays in propagating data, so sleep and retry
    hook = cli_ctx.get_progress_controller(True)
    hook.add(message='Waiting for AAD role to delete', value=0, total_val=1.0)
    logger.info('Waiting for AAD role to delete')
    for x in range(0, 10):
        hook.add(message='Waiting for AAD role to delete',
                 value=0.1 * x, total_val=1.0)
        try:
            delete_role_assignments(cli_ctx,
                                    role=role,
                                    assignee=service_principal,
                                    scope=scope)
            break
        except CLIError as ex:
            raise ex
        except CloudError as ex:
            logger.info(ex)
        time.sleep(delay + delay * x)
    else:
        return False
    hook.add(message='AAD role deletion done', value=1.0, total_val=1.0)
    logger.info('AAD role deletion done')
    return True


# pylint: disable=too-many-locals
def store_acs_service_principal(subscription_id, client_secret, service_principal,
                                file_name='acsServicePrincipal.json'):
    obj = {}
    if client_secret:
        obj['client_secret'] = client_secret
    if service_principal:
        obj['service_principal'] = service_principal

    config_path = os.path.join(get_config_dir(), file_name)
    full_config = load_service_principals(config_path=config_path)
    if not full_config:
        full_config = {}
    full_config[subscription_id] = obj

    with os.fdopen(os.open(config_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600),
                   'w+') as spFile:
        json.dump(full_config, spFile)


def load_acs_service_principal(subscription_id, file_name='acsServicePrincipal.json'):
    config_path = os.path.join(get_config_dir(), file_name)
    config = load_service_principals(config_path)
    if not config:
        return None
    return config.get(subscription_id)


def load_service_principals(config_path):
    if not os.path.exists(config_path):
        return None
    fd = os.open(config_path, os.O_RDONLY)
    try:
        with os.fdopen(fd) as f:
            return shell_safe_json_parse(f.read())
    except:  # pylint: disable=bare-except
        return None


def create_application(client, display_name, homepage, identifier_uris,
                       available_to_other_tenants=False, password=None, reply_urls=None,
                       key_value=None, key_type=None, key_usage=None, start_date=None,
                       end_date=None):
    from azure.graphrbac.models import GraphErrorException
    password_creds, key_creds = _build_application_creds(password=password, key_value=key_value, key_type=key_type,
                                                         key_usage=key_usage, start_date=start_date, end_date=end_date)

    app_create_param = ApplicationCreateParameters(available_to_other_tenants=available_to_other_tenants,
                                                   display_name=display_name,
                                                   identifier_uris=identifier_uris,
                                                   homepage=homepage,
                                                   reply_urls=reply_urls,
                                                   key_credentials=key_creds,
                                                   password_credentials=password_creds)
    try:
        return client.create(app_create_param)
    except GraphErrorException as ex:
        if 'insufficient privileges' in str(ex).lower():
            link = 'https://docs.microsoft.com/azure/azure-resource-manager/resource-group-create-service-principal-portal'  # pylint: disable=line-too-long
            raise CLIError("Directory permission is needed for the current user to register the application. "
                           "For how to configure, please refer '{}'. Original error: {}".format(link, ex))
        raise


def _build_application_creds(password=None, key_value=None, key_type=None,
                             key_usage=None, start_date=None, end_date=None):
    if password and key_value:
        raise CLIError(
            'specify either --password or --key-value, but not both.')

    if not start_date:
        start_date = datetime.datetime.utcnow()
    elif isinstance(start_date, str):
        start_date = parse(start_date)

    if not end_date:
        end_date = start_date + relativedelta(years=1)
    elif isinstance(end_date, str):
        end_date = parse(end_date)

    key_type = key_type or 'AsymmetricX509Cert'
    key_usage = key_usage or 'Verify'

    password_creds = None
    key_creds = None
    if password:
        password_creds = [PasswordCredential(start_date=start_date, end_date=end_date,
                                             key_id=str(uuid.uuid4()), value=password)]
    elif key_value:
        key_creds = [KeyCredential(start_date=start_date, end_date=end_date, value=key_value,
                                   key_id=str(uuid.uuid4()), usage=key_usage, type=key_type)]

    return (password_creds, key_creds)


def create_service_principal(cli_ctx, identifier, resolve_app=True, rbac_client=None):
    if rbac_client is None:
        rbac_client = get_graph_rbac_management_client(cli_ctx)

    if resolve_app:
        try:
            uuid.UUID(identifier)
            result = list(rbac_client.applications.list(
                filter="appId eq '{}'".format(identifier)))
        except ValueError:
            result = list(rbac_client.applications.list(
                filter="identifierUris/any(s:s eq '{}')".format(identifier)))

        if not result:  # assume we get an object id
            result = [rbac_client.applications.get(identifier)]
        app_id = result[0].app_id
    else:
        app_id = identifier

    return rbac_client.service_principals.create(ServicePrincipalCreateParameters(app_id=app_id, account_enabled=True))


def delete_role_assignments(cli_ctx, ids=None, assignee=None, role=None, resource_group_name=None,
                            scope=None, include_inherited=False, yes=None):
    factory = get_auth_management_client(cli_ctx, scope)
    assignments_client = factory.role_assignments
    definitions_client = factory.role_definitions
    ids = ids or []
    if ids:
        if assignee or role or resource_group_name or scope or include_inherited:
            raise CLIError(
                'When assignment ids are used, other parameter values are not required')
        for i in ids:
            assignments_client.delete_by_id(i)
        return
    if not any([ids, assignee, role, resource_group_name, scope, assignee, yes]):
        msg = 'This will delete all role assignments under the subscription. Are you sure?'
        if not prompt_y_n(msg, default="n"):
            return

    scope = build_role_scope(resource_group_name, scope,
                             assignments_client.config.subscription_id)
    assignments = _search_role_assignments(cli_ctx, assignments_client, definitions_client,
                                           scope, assignee, role, include_inherited,
                                           include_groups=False)

    if assignments:
        for a in assignments:
            assignments_client.delete_by_id(a.id)


def _delete_role_assignments(cli_ctx, role, service_principal, delay=2, scope=None):
    # AAD can have delays in propagating data, so sleep and retry
    hook = cli_ctx.get_progress_controller(True)
    hook.add(message='Waiting for AAD role to delete', value=0, total_val=1.0)
    logger.info('Waiting for AAD role to delete')
    for x in range(0, 10):
        hook.add(message='Waiting for AAD role to delete',
                 value=0.1 * x, total_val=1.0)
        try:
            delete_role_assignments(cli_ctx,
                                    role=role,
                                    assignee=service_principal,
                                    scope=scope)
            break
        except CLIError as ex:
            raise ex
        except CloudError as ex:
            logger.info(ex)
        time.sleep(delay + delay * x)
    else:
        return False
    hook.add(message='AAD role deletion done', value=1.0, total_val=1.0)
    logger.info('AAD role deletion done')
    return True


def _search_role_assignments(cli_ctx, assignments_client, definitions_client,
                             scope, assignee, role, include_inherited, include_groups):
    assignee_object_id = None
    if assignee:
        assignee_object_id = resolve_object_id(cli_ctx, assignee)

    # always use "scope" if provided, so we can get assignments beyond subscription e.g. management groups
    if scope:
        assignments = list(assignments_client.list_for_scope(
            scope=scope, filter='atScope()'))
    elif assignee_object_id:
        if include_groups:
            f = "assignedTo('{}')".format(assignee_object_id)
        else:
            f = "principalId eq '{}'".format(assignee_object_id)
        assignments = list(assignments_client.list(filter=f))
    else:
        assignments = list(assignments_client.list())

    if assignments:
        assignments = [a for a in assignments if (
            not scope or
            include_inherited and re.match(_get_role_property(a, 'scope'), scope, re.I) or
            _get_role_property(a, 'scope').lower() == scope.lower()
        )]

        if role:
            role_id = resolve_role_id(role, scope, definitions_client)
            assignments = [i for i in assignments if _get_role_property(
                i, 'role_definition_id') == role_id]

        if assignee_object_id:
            assignments = [i for i in assignments if _get_role_property(
                i, 'principal_id') == assignee_object_id]

    return assignments


def _get_role_property(obj, property_name):
    if isinstance(obj, dict):
        return obj[property_name]
    return getattr(obj, property_name)


def subnet_role_assignment_exists(cli_ctx, scope):
    network_contributor_role_id = "4d97b98b-1d4f-4787-a291-c67834d212e7"

    factory = get_auth_management_client(cli_ctx, scope)
    assignments_client = factory.role_assignments

    for i in assignments_client.list_for_scope(scope=scope, filter='atScope()'):
        if i.scope == scope and i.role_definition_id.endswith(network_contributor_role_id):
            return True
    return False


_re_user_assigned_identity_resource_id = re.compile(
    r'/subscriptions/(.*?)/resourcegroups/(.*?)/providers/microsoft.managedidentity/userassignedidentities/(.*)',
    flags=re.IGNORECASE)


def _get_user_assigned_identity(cli_ctx, resource_id):
    resource_id = resource_id.lower()
    match = _re_user_assigned_identity_resource_id.search(resource_id)
    if match:
        subscription_id = match.group(1)
        resource_group_name = match.group(2)
        identity_name = match.group(3)
        msi_client = get_msi_client(cli_ctx, subscription_id)
        try:
            identity = msi_client.user_assigned_identities.get(resource_group_name=resource_group_name,
                                                               resource_name=identity_name)
        except CloudError as ex:
            if 'was not found' in ex.message:
                raise CLIError("Identity {} not found.".format(resource_id))
            raise CLIError(ex.message)
        return identity
    raise CLIError(
        "Cannot parse identity name from provided resource id {}.".format(resource_id))


def aks_browse(
    cmd,
    client,
    resource_group_name,
    name,
    disable_browser=False,
    listen_address="127.0.0.1",
    listen_port="8001",
):
    from azure.cli.command_modules.acs.custom import _aks_browse

    return _aks_browse(
        cmd,
        client,
        resource_group_name,
        name,
        disable_browser,
        listen_address,
        listen_port,
        CUSTOM_MGMT_AKS_PREVIEW,
    )


def aks_maintenanceconfiguration_list(
    cmd,
    client,
    resource_group_name,
    cluster_name
):
    return client.list_by_managed_cluster(resource_group_name, cluster_name)


def aks_maintenanceconfiguration_show(
    cmd,
    client,
    resource_group_name,
    cluster_name,
    config_name
):
    logger.warning('resource_group_name: %s, cluster_name: %s, config_name: %s ',
                   resource_group_name, cluster_name, config_name)
    return client.get(resource_group_name, cluster_name, config_name)


def aks_maintenanceconfiguration_delete(
    cmd,
    client,
    resource_group_name,
    cluster_name,
    config_name
):
    logger.warning('resource_group_name: %s, cluster_name: %s, config_name: %s ',
                   resource_group_name, cluster_name, config_name)
    return client.delete(resource_group_name, cluster_name, config_name)


def aks_maintenanceconfiguration_add(
    cmd,
    client,
    resource_group_name,
    cluster_name,
    config_name,
    config_file,
    weekday,
    start_hour
):
    configs = client.list_by_managed_cluster(resource_group_name, cluster_name)
    for config in configs:
        if config.name == config_name:
            raise CLIError("Maintenance configuration '{}' already exists, please try a different name, "
                           "use 'aks maintenanceconfiguration list' to get current list of maitenance configurations".format(config_name))
    return aks_maintenanceconfiguration_update_internal(cmd, client, resource_group_name, cluster_name, config_name, config_file, weekday, start_hour)


def aks_maintenanceconfiguration_update(
    cmd,
    client,
    resource_group_name,
    cluster_name,
    config_name,
    config_file,
    weekday,
    start_hour
):
    configs = client.list_by_managed_cluster(resource_group_name, cluster_name)
    found = False
    for config in configs:
        if config.name == config_name:
            found = True
            break
    if not found:
        raise CLIError("Maintenance configuration '{}' doesn't exist."
                       "use 'aks maintenanceconfiguration list' to get current list of maitenance configurations".format(config_name))

    return aks_maintenanceconfiguration_update_internal(cmd, client, resource_group_name, cluster_name, config_name, config_file, weekday, start_hour)


# pylint: disable=too-many-locals
def aks_create(
    cmd,
    client,
    resource_group_name,
    name,
    ssh_key_value,
    location=None,
    kubernetes_version="",
    tags=None,
    dns_name_prefix=None,
    node_osdisk_diskencryptionset_id=None,
    disable_local_accounts=False,
    disable_rbac=None,
    edge_zone=None,
    admin_username="azureuser",
    generate_ssh_keys=False,
    no_ssh_key=False,
    pod_cidr=None,
    service_cidr=None,
    dns_service_ip=None,
    docker_bridge_address=None,
    load_balancer_sku=None,
    load_balancer_managed_outbound_ip_count=None,
    load_balancer_outbound_ips=None,
    load_balancer_outbound_ip_prefixes=None,
    load_balancer_outbound_ports=None,
    load_balancer_idle_timeout=None,
    load_balancer_backend_pool_type=None,
    nat_gateway_managed_outbound_ip_count=None,
    nat_gateway_idle_timeout=None,
    outbound_type=None,
    network_plugin=None,
    network_plugin_mode=None,
    network_policy=None,
    kube_proxy_config=None,
    auto_upgrade_channel=None,
    cluster_autoscaler_profile=None,
    uptime_sla=False,
    fqdn_subdomain=None,
    api_server_authorized_ip_ranges=None,
    enable_private_cluster=False,
    private_dns_zone=None,
    disable_public_fqdn=False,
    service_principal=None,
    client_secret=None,
    enable_managed_identity=True,
    assign_identity=None,
    assign_kubelet_identity=None,
    enable_aad=False,
    enable_azure_rbac=False,
    aad_admin_group_object_ids=None,
    aad_client_app_id=None,
    aad_server_app_id=None,
    aad_server_app_secret=None,
    aad_tenant_id=None,
    windows_admin_username=None,
    windows_admin_password=None,
    enable_ahub=False,
    enable_windows_gmsa=False,
    gmsa_dns_server=None,
    gmsa_root_domain_name=None,
    attach_acr=None,
    skip_subnet_role_assignment=False,
    node_resource_group=None,
    enable_defender=False,
    defender_config=None,
    # addons
    enable_addons=None,
    workspace_resource_id=None,
    enable_msi_auth_for_monitoring=False,
    aci_subnet_name=None,
    appgw_name=None,
    appgw_subnet_cidr=None,
    appgw_id=None,
    appgw_subnet_id=None,
    appgw_watch_namespace=None,
    enable_sgxquotehelper=False,
    enable_secret_rotation=False,
    rotation_poll_interval=None,
    # nodepool paramerters
    nodepool_name="nodepool1",
    node_vm_size=None,
    os_sku=None,
    snapshot_id=None,
    vnet_subnet_id=None,
    pod_subnet_id=None,
    enable_node_public_ip=False,
    node_public_ip_prefix_id=None,
    enable_cluster_autoscaler=False,
    min_count=None,
    max_count=None,
    node_count=3,
    nodepool_tags=None,
    nodepool_labels=None,
    node_osdisk_type=None,
    node_osdisk_size=0,
    vm_set_type=None,
    # TODO: remove node_zones after cli 2.38.0 release
    node_zones=None,
    zones=None,
    ppg=None,
    max_pods=0,
    enable_encryption_at_host=False,
    enable_ultra_ssd=False,
    enable_fips_image=False,
    kubelet_config=None,
    linux_os_config=None,
    no_wait=False,
    yes=False,
    aks_custom_headers=None,
    # extensions
    # managed cluster
    http_proxy_config=None,
    ip_families=None,
    pod_cidrs=None,
    service_cidrs=None,
    load_balancer_managed_outbound_ipv6_count=None,
    enable_pod_security_policy=False,
    enable_pod_identity=False,
    enable_pod_identity_with_kubenet=False,
    enable_workload_identity=None,
    enable_oidc_issuer=False,
    enable_azure_keyvault_kms=False,
    azure_keyvault_kms_key_id=None,
    azure_keyvault_kms_key_vault_network_access=None,
    azure_keyvault_kms_key_vault_resource_id=None,
    enable_image_cleaner=False,
    image_cleaner_interval_hours=None,
    cluster_snapshot_id=None,
    disk_driver_version=None,
    disable_disk_driver=False,
    disable_file_driver=False,
    enable_blob_driver=None,
    disable_snapshot_controller=False,
    enable_apiserver_vnet_integration=False,
    apiserver_subnet_id=None,
    dns_zone_resource_id=None,
    enable_keda=False,
    enable_node_restriction=False,
    enable_vpa=False,
    # nodepool
    host_group_id=None,
    crg_id=None,
    message_of_the_day=None,
    gpu_instance_profile=None,
    workload_runtime=None,
    enable_custom_ca_trust=False,
):
    # DO NOT MOVE: get all the original parameters and save them as a dictionary
    raw_parameters = locals()

    from azure.cli.command_modules.acs._consts import DecoratorEarlyExitException
    from azext_aks_preview.managed_cluster_decorator import AKSPreviewManagedClusterCreateDecorator

    # decorator pattern
    aks_create_decorator = AKSPreviewManagedClusterCreateDecorator(
        cmd=cmd,
        client=client,
        raw_parameters=raw_parameters,
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
    )
    try:
        # construct mc profile
        mc = aks_create_decorator.construct_mc_profile_preview()
    except DecoratorEarlyExitException:
        # exit gracefully
        return None
    # send request to create a real managed cluster
    return aks_create_decorator.create_mc(mc)


# pylint: disable=too-many-locals
def aks_update(
    cmd,
    client,
    resource_group_name,
    name,
    tags=None,
    disable_local_accounts=False,
    enable_local_accounts=False,
    load_balancer_managed_outbound_ip_count=None,
    load_balancer_outbound_ips=None,
    load_balancer_outbound_ip_prefixes=None,
    load_balancer_outbound_ports=None,
    load_balancer_idle_timeout=None,
    load_balancer_backend_pool_type=None,
    nat_gateway_managed_outbound_ip_count=None,
    nat_gateway_idle_timeout=None,
    auto_upgrade_channel=None,
    cluster_autoscaler_profile=None,
    uptime_sla=False,
    no_uptime_sla=False,
    api_server_authorized_ip_ranges=None,
    enable_public_fqdn=False,
    disable_public_fqdn=False,
    enable_managed_identity=False,
    assign_identity=None,
    assign_kubelet_identity=None,
    enable_aad=False,
    enable_azure_rbac=False,
    disable_azure_rbac=False,
    aad_tenant_id=None,
    aad_admin_group_object_ids=None,
    windows_admin_password=None,
    enable_ahub=False,
    disable_ahub=False,
    enable_windows_gmsa=False,
    gmsa_dns_server=None,
    gmsa_root_domain_name=None,
    attach_acr=None,
    detach_acr=None,
    enable_defender=False,
    disable_defender=False,
    defender_config=None,
    # addons
    enable_secret_rotation=False,
    disable_secret_rotation=False,
    rotation_poll_interval=None,
    # nodepool paramerters
    enable_cluster_autoscaler=False,
    disable_cluster_autoscaler=False,
    update_cluster_autoscaler=False,
    min_count=None,
    max_count=None,
    nodepool_labels=None,
    no_wait=False,
    yes=False,
    aks_custom_headers=None,
    # extensions
    # managed cluster
    http_proxy_config=None,
    load_balancer_managed_outbound_ipv6_count=None,
    enable_pod_security_policy=False,
    disable_pod_security_policy=False,
    enable_pod_identity=False,
    enable_pod_identity_with_kubenet=False,
    disable_pod_identity=False,
    enable_workload_identity=None,
    enable_oidc_issuer=False,
    enable_azure_keyvault_kms=False,
    disable_azure_keyvault_kms=False,
    azure_keyvault_kms_key_id=None,
    azure_keyvault_kms_key_vault_network_access=None,
    azure_keyvault_kms_key_vault_resource_id=None,
    enable_image_cleaner=False,
    disable_image_cleaner=False,
    image_cleaner_interval_hours=None,
    enable_disk_driver=False,
    disk_driver_version=None,
    disable_disk_driver=False,
    enable_file_driver=False,
    disable_file_driver=False,
    enable_blob_driver=None,
    disable_blob_driver=None,
    enable_snapshot_controller=False,
    disable_snapshot_controller=False,
    enable_apiserver_vnet_integration=False,
    apiserver_subnet_id=None,
    enable_keda=False,
    disable_keda=False,
    enable_node_restriction=False,
    disable_node_restriction=False,
    enable_private_cluster=False,
    disable_private_cluster=False,
    private_dns_zone=None,
    enable_azuremonitormetrics=False,
    azure_monitor_workspace_resource_id=None,
    ksm_metric_labels_allow_list=None,
    ksm_metric_annotations_allow_list=None,
    grafana_resource_id=None,
    disable_azuremonitormetrics=False,
    enable_vpa=False,
    disable_vpa=False,
    cluster_snapshot_id=None,
):
    # DO NOT MOVE: get all the original parameters and save them as a dictionary
    raw_parameters = locals()

    from azure.cli.command_modules.acs._consts import DecoratorEarlyExitException
    from azext_aks_preview.managed_cluster_decorator import AKSPreviewManagedClusterUpdateDecorator

    # decorator pattern
    aks_update_decorator = AKSPreviewManagedClusterUpdateDecorator(
        cmd=cmd,
        client=client,
        raw_parameters=raw_parameters,
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
    )
    try:
        # update mc profile
        mc = aks_update_decorator.update_mc_profile_preview()
    except DecoratorEarlyExitException:
        # exit gracefully
        return None
    # send request to update the real managed cluster
    return aks_update_decorator.update_mc(mc)


# pylint: disable=unused-argument
def aks_show(cmd, client, resource_group_name, name):
    mc = client.get(resource_group_name, name)
    return _remove_nulls([mc])[0]


def _remove_nulls(managed_clusters):
    """
    Remove some often-empty fields from a list of ManagedClusters, so the JSON representation
    doesn't contain distracting null fields.

    This works around a quirk of the SDK for python behavior. These fields are not sent
    by the server, but get recreated by the CLI's own "to_dict" serialization.
    """
    attrs = ['tags']
    ap_attrs = ['os_disk_size_gb', 'vnet_subnet_id']
    sp_attrs = ['secret']
    for managed_cluster in managed_clusters:
        for attr in attrs:
            if getattr(managed_cluster, attr, None) is None:
                delattr(managed_cluster, attr)
        if managed_cluster.agent_pool_profiles is not None:
            for ap_profile in managed_cluster.agent_pool_profiles:
                for attr in ap_attrs:
                    if getattr(ap_profile, attr, None) is None:
                        delattr(ap_profile, attr)
        for attr in sp_attrs:
            if getattr(managed_cluster.service_principal_profile, attr, None) is None:
                delattr(managed_cluster.service_principal_profile, attr)
    return managed_clusters


def aks_get_credentials(cmd,    # pylint: disable=unused-argument
                        client,
                        resource_group_name,
                        name,
                        admin=False,
                        user='clusterUser',
                        path=os.path.join(os.path.expanduser(
                            '~'), '.kube', 'config'),
                        overwrite_existing=False,
                        context_name=None,
                        public_fqdn=False,
                        credential_format=None):
    credentialResults = None
    serverType = None
    if public_fqdn:
        serverType = 'public'
    if credential_format:
        credential_format = credential_format.lower()
        if admin:
            raise InvalidArgumentValueError("--format can only be specified when requesting clusterUser credential.")
    if admin:
        credentialResults = client.list_cluster_admin_credentials(
            resource_group_name, name, serverType)
    else:
        if user.lower() == 'clusteruser':
            credentialResults = client.list_cluster_user_credentials(
                resource_group_name, name, serverType, credential_format)
        elif user.lower() == 'clustermonitoringuser':
            credentialResults = client.list_cluster_monitoring_user_credentials(
                resource_group_name, name, serverType)
        else:
            raise CLIError("The user is invalid.")
    if not credentialResults:
        raise CLIError("No Kubernetes credentials found.")

    try:
        kubeconfig = credentialResults.kubeconfigs[0].value.decode(
            encoding='UTF-8')
        print_or_merge_credentials(
            path, kubeconfig, overwrite_existing, context_name)
    except (IndexError, ValueError):
        raise CLIError("Fail to find kubeconfig file.")


def aks_scale(cmd,  # pylint: disable=unused-argument
              client,
              resource_group_name,
              name,
              node_count,
              nodepool_name="",
              no_wait=False):
    instance = client.get(resource_group_name, name)
    _fill_defaults_for_pod_identity_profile(instance.pod_identity_profile)

    if len(instance.agent_pool_profiles) > 1 and nodepool_name == "":
        raise CLIError('There are more than one node pool in the cluster. '
                       'Please specify nodepool name or use az aks nodepool command to scale node pool')

    for agent_profile in instance.agent_pool_profiles:
        if agent_profile.name == nodepool_name or (nodepool_name == "" and len(instance.agent_pool_profiles) == 1):
            if agent_profile.enable_auto_scaling:
                raise CLIError(
                    "Cannot scale cluster autoscaler enabled node pool.")

            agent_profile.count = int(node_count)  # pylint: disable=no-member
            # null out the SP profile because otherwise validation complains
            instance.service_principal_profile = None
            return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, name, instance)
    raise CLIError('The nodepool "{}" was not found.'.format(nodepool_name))


def aks_upgrade(cmd,    # pylint: disable=unused-argument, too-many-return-statements
                client,
                resource_group_name,
                name,
                kubernetes_version='',
                control_plane_only=False,
                no_wait=False,
                node_image_only=False,
                cluster_snapshot_id=None,
                aks_custom_headers=None,
                yes=False):
    msg = 'Kubernetes may be unavailable during cluster upgrades.\n Are you sure you want to perform this operation?'
    if not yes and not prompt_y_n(msg, default="n"):
        return None

    instance = client.get(resource_group_name, name)
    _fill_defaults_for_pod_identity_profile(instance.pod_identity_profile)

    vmas_cluster = False
    for agent_profile in instance.agent_pool_profiles:
        if agent_profile.type.lower() == "availabilityset":
            vmas_cluster = True
            break

    if kubernetes_version != '' and node_image_only:
        raise CLIError('Conflicting flags. Upgrading the Kubernetes version will also upgrade node image version. '
                       'If you only want to upgrade the node version please use the "--node-image-only" option only.')

    if node_image_only:
        msg = "This node image upgrade operation will run across every node pool in the cluster " \
              "and might take a while. Do you wish to continue?"
        if not yes and not prompt_y_n(msg, default="n"):
            return None

        # This only provide convenience for customer at client side so they can run az aks upgrade to upgrade all
        # nodepools of a cluster. The SDK only support upgrade single nodepool at a time.
        for agent_pool_profile in instance.agent_pool_profiles:
            if vmas_cluster:
                raise CLIError('This cluster is not using VirtualMachineScaleSets. Node image upgrade only operation '
                               'can only be applied on VirtualMachineScaleSets cluster.')
            agent_pool_client = cf_agent_pools(cmd.cli_ctx)
            _upgrade_single_nodepool_image_version(
                True, agent_pool_client, resource_group_name, name, agent_pool_profile.name, None)
        mc = client.get(resource_group_name, name)
        return _remove_nulls([mc])[0]

    if cluster_snapshot_id:
        CreationData = cmd.get_models(
            "CreationData",
            resource_type=CUSTOM_MGMT_AKS_PREVIEW,
            operation_group="managed_clusters",
        )
        instance.creation_data = CreationData(
            source_resource_id=cluster_snapshot_id
        )
        mcsnapshot = get_cluster_snapshot_by_snapshot_id(cmd.cli_ctx, cluster_snapshot_id)
        kubernetes_version = mcsnapshot.managed_cluster_properties_read_only.kubernetes_version

    if instance.kubernetes_version == kubernetes_version:
        if instance.provisioning_state == "Succeeded":
            logger.warning("The cluster is already on version %s and is not in a failed state. No operations "
                           "will occur when upgrading to the same version if the cluster is not in a failed state.",
                           instance.kubernetes_version)
        elif instance.provisioning_state == "Failed":
            logger.warning("Cluster currently in failed state. Proceeding with upgrade to existing version %s to "
                           "attempt resolution of failed cluster state.", instance.kubernetes_version)

    upgrade_all = False
    instance.kubernetes_version = kubernetes_version

    # for legacy clusters, we always upgrade node pools with CCP.
    if instance.max_agent_pools < 8 or vmas_cluster:
        if control_plane_only:
            msg = ("Legacy clusters do not support control plane only upgrade. All node pools will be "
                   "upgraded to {} as well. Continue?").format(instance.kubernetes_version)
            if not yes and not prompt_y_n(msg, default="n"):
                return None
        upgrade_all = True
    else:
        if not control_plane_only:
            msg = ("Since control-plane-only argument is not specified, this will upgrade the control plane "
                   "AND all nodepools to version {}. Continue?").format(instance.kubernetes_version)
            if not yes and not prompt_y_n(msg, default="n"):
                return None
            upgrade_all = True
        else:
            msg = ("Since control-plane-only argument is specified, this will upgrade only the control plane to {}. "
                   "Node pool will not change. Continue?").format(instance.kubernetes_version)
            if not yes and not prompt_y_n(msg, default="n"):
                return None

    if upgrade_all:
        for agent_profile in instance.agent_pool_profiles:
            agent_profile.orchestrator_version = kubernetes_version
            agent_profile.creation_data = None

    # null out the SP profile because otherwise validation complains
    instance.service_principal_profile = None

    headers = get_aks_custom_headers(aks_custom_headers)

    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, name, instance, headers=headers)


def _upgrade_single_nodepool_image_version(no_wait, client, resource_group_name, cluster_name, nodepool_name, snapshot_id=None):
    headers = {}
    if snapshot_id:
        headers["AKSSnapshotId"] = snapshot_id

    return sdk_no_wait(no_wait, client.begin_upgrade_node_image_version, resource_group_name, cluster_name, nodepool_name, headers=headers)


def _ensure_aks_acr(cli_ctx,
                    client_id,
                    acr_name_or_id,
                    subscription_id,    # pylint: disable=unused-argument
                    detach=False):
    from msrestazure.tools import is_valid_resource_id, parse_resource_id

    # Check if the ACR exists by resource ID.
    if is_valid_resource_id(acr_name_or_id):
        try:
            parsed_registry = parse_resource_id(acr_name_or_id)
            acr_client = get_container_registry_client(
                cli_ctx, subscription_id=parsed_registry['subscription'])
            registry = acr_client.registries.get(
                parsed_registry['resource_group'], parsed_registry['name'])
        except CloudError as ex:
            raise CLIError(ex.message)
        _ensure_aks_acr_role_assignment(
            cli_ctx, client_id, registry.id, detach)
        return

    # Check if the ACR exists by name accross all resource groups.
    registry_name = acr_name_or_id
    registry_resource = 'Microsoft.ContainerRegistry/registries'
    try:
        registry = get_resource_by_name(
            cli_ctx, registry_name, registry_resource)
    except CloudError as ex:
        if 'was not found' in ex.message:
            raise CLIError(
                "ACR {} not found. Have you provided the right ACR name?".format(registry_name))
        raise CLIError(ex.message)
    _ensure_aks_acr_role_assignment(cli_ctx, client_id, registry.id, detach)
    return


def _ensure_aks_acr_role_assignment(cli_ctx,
                                    client_id,
                                    registry_id,
                                    detach=False):
    if detach:
        if not _delete_role_assignments(cli_ctx,
                                        'acrpull',
                                        client_id,
                                        scope=registry_id):
            raise CLIError('Could not delete role assignments for ACR. '
                           'Are you an Owner on this subscription?')
        return

    if not add_role_assignment(cli_ctx,
                               'acrpull',
                               client_id,
                               scope=registry_id):
        raise CLIError('Could not create a role assignment for ACR. '
                       'Are you an Owner on this subscription?')
    return


def aks_agentpool_show(cmd,     # pylint: disable=unused-argument
                       client,
                       resource_group_name,
                       cluster_name,
                       nodepool_name):
    instance = client.get(resource_group_name, cluster_name, nodepool_name)
    return instance


def aks_agentpool_list(cmd,     # pylint: disable=unused-argument
                       client,
                       resource_group_name,
                       cluster_name):
    return client.list(resource_group_name, cluster_name)


# pylint: disable=too-many-locals
def aks_agentpool_add(
    cmd,
    client,
    resource_group_name,
    cluster_name,
    nodepool_name,
    kubernetes_version=None,
    node_vm_size=None,
    os_type=None,
    os_sku=None,
    snapshot_id=None,
    vnet_subnet_id=None,
    pod_subnet_id=None,
    enable_node_public_ip=False,
    node_public_ip_prefix_id=None,
    enable_cluster_autoscaler=False,
    min_count=None,
    max_count=None,
    node_count=3,
    priority=CONST_SCALE_SET_PRIORITY_REGULAR,
    eviction_policy=CONST_SPOT_EVICTION_POLICY_DELETE,
    spot_max_price=float("nan"),
    labels=None,
    tags=None,
    node_taints=None,
    node_osdisk_type=None,
    node_osdisk_size=0,
    max_surge=None,
    mode=CONST_NODEPOOL_MODE_USER,
    scale_down_mode=CONST_SCALE_DOWN_MODE_DELETE,
    max_pods=0,
    # TODO: remove node_zones after cli 2.38.0 release
    node_zones=None,
    zones=None,
    ppg=None,
    enable_encryption_at_host=False,
    enable_ultra_ssd=False,
    enable_fips_image=False,
    kubelet_config=None,
    linux_os_config=None,
    no_wait=False,
    aks_custom_headers=None,
    # extensions
    host_group_id=None,
    crg_id=None,
    message_of_the_day=None,
    workload_runtime=None,
    gpu_instance_profile=None,
    enable_custom_ca_trust=False,
):
    # DO NOT MOVE: get all the original parameters and save them as a dictionary
    raw_parameters = locals()

    # decorator pattern
    from azure.cli.command_modules.acs._consts import AgentPoolDecoratorMode, DecoratorEarlyExitException
    from azext_aks_preview.agentpool_decorator import AKSPreviewAgentPoolAddDecorator
    aks_agentpool_add_decorator = AKSPreviewAgentPoolAddDecorator(
        cmd=cmd,
        client=client,
        raw_parameters=raw_parameters,
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        agentpool_decorator_mode=AgentPoolDecoratorMode.STANDALONE,
    )
    try:
        # construct agentpool profile
        agentpool = aks_agentpool_add_decorator.construct_agentpool_profile_preview()
    except DecoratorEarlyExitException:
        # exit gracefully
        return None
    # send request to add a real agentpool
    return aks_agentpool_add_decorator.add_agentpool(agentpool)


# pylint: disable=too-many-locals
def aks_agentpool_update(
    cmd,
    client,
    resource_group_name,
    cluster_name,
    nodepool_name,
    enable_cluster_autoscaler=False,
    disable_cluster_autoscaler=False,
    update_cluster_autoscaler=False,
    min_count=None,
    max_count=None,
    labels=None,
    tags=None,
    node_taints=None,
    max_surge=None,
    mode=None,
    scale_down_mode=None,
    no_wait=False,
    aks_custom_headers=None,
    # extensions
    enable_custom_ca_trust=False,
    disable_custom_ca_trust=False,
):
    # DO NOT MOVE: get all the original parameters and save them as a dictionary
    raw_parameters = locals()

    # decorator pattern
    from azure.cli.command_modules.acs._consts import AgentPoolDecoratorMode, DecoratorEarlyExitException
    from azext_aks_preview.agentpool_decorator import AKSPreviewAgentPoolUpdateDecorator
    aks_agentpool_update_decorator = AKSPreviewAgentPoolUpdateDecorator(
        cmd=cmd,
        client=client,
        raw_parameters=raw_parameters,
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        agentpool_decorator_mode=AgentPoolDecoratorMode.STANDALONE,
    )
    try:
        # update agentpool profile
        agentpool = aks_agentpool_update_decorator.update_agentpool_profile_preview()
    except DecoratorEarlyExitException:
        # exit gracefully
        return None
    # send request to update the real agentpool
    return aks_agentpool_update_decorator.update_agentpool(agentpool)


def aks_agentpool_scale(cmd,    # pylint: disable=unused-argument
                        client,
                        resource_group_name,
                        cluster_name,
                        nodepool_name,
                        node_count=3,
                        no_wait=False):
    instance = client.get(resource_group_name, cluster_name, nodepool_name)
    new_node_count = int(node_count)
    if instance.enable_auto_scaling:
        raise CLIError("Cannot scale cluster autoscaler enabled node pool.")
    if new_node_count == instance.count:
        raise CLIError(
            "The new node count is the same as the current node count.")
    instance.count = new_node_count  # pylint: disable=no-member
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, nodepool_name, instance)


def aks_agentpool_upgrade(cmd,  # pylint: disable=unused-argument
                          client,
                          resource_group_name,
                          cluster_name,
                          nodepool_name,
                          kubernetes_version='',
                          no_wait=False,
                          node_image_only=False,
                          max_surge=None,
                          aks_custom_headers=None,
                          snapshot_id=None):
    AgentPoolUpgradeSettings = cmd.get_models(
        "AgentPoolUpgradeSettings",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="agent_pools",
    )
    CreationData = cmd.get_models(
        "CreationData",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    if kubernetes_version != '' and node_image_only:
        raise CLIError('Conflicting flags. Upgrading the Kubernetes version will also upgrade node image version.'
                       'If you only want to upgrade the node version please use the "--node-image-only" option only.')

    if node_image_only:
        return _upgrade_single_nodepool_image_version(no_wait,
                                                      client,
                                                      resource_group_name,
                                                      cluster_name,
                                                      nodepool_name,
                                                      snapshot_id)

    creationData = None
    if snapshot_id:
        snapshot = get_nodepool_snapshot_by_snapshot_id(cmd.cli_ctx, snapshot_id)
        if not kubernetes_version and not node_image_only:
            kubernetes_version = snapshot.kubernetes_version

        creationData = CreationData(
            source_resource_id=snapshot_id
        )

    instance = client.get(resource_group_name, cluster_name, nodepool_name)
    instance.orchestrator_version = kubernetes_version
    instance.creation_data = creationData

    if not instance.upgrade_settings:
        instance.upgrade_settings = AgentPoolUpgradeSettings()

    if max_surge:
        instance.upgrade_settings.max_surge = max_surge

    headers = get_aks_custom_headers(aks_custom_headers)

    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, nodepool_name, instance, headers=headers)


def aks_agentpool_get_upgrade_profile(cmd,   # pylint: disable=unused-argument
                                      client,
                                      resource_group_name,
                                      cluster_name,
                                      nodepool_name):
    return client.get_upgrade_profile(resource_group_name, cluster_name, nodepool_name)


def aks_agentpool_stop(cmd,   # pylint: disable=unused-argument
                       client,
                       resource_group_name,
                       cluster_name,
                       nodepool_name,
                       aks_custom_headers=None,
                       no_wait=False):
    PowerState = cmd.get_models(
        "PowerState",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    agentpool_exists = False
    instances = client.list(resource_group_name, cluster_name)
    for agentpool_profile in instances:
        if agentpool_profile.name.lower() == nodepool_name.lower():
            agentpool_exists = True
            break

    if not agentpool_exists:
        raise InvalidArgumentValueError(
            "Node pool {} doesnt exist, use 'aks nodepool list' to get current node pool list".format(nodepool_name))

    instance = client.get(resource_group_name, cluster_name, nodepool_name)
    power_state = PowerState(code="Stopped")
    instance.power_state = power_state
    headers = get_aks_custom_headers(aks_custom_headers)
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, nodepool_name, instance, headers=headers)


def aks_agentpool_start(cmd,   # pylint: disable=unused-argument
                        client,
                        resource_group_name,
                        cluster_name,
                        nodepool_name,
                        aks_custom_headers=None,
                        no_wait=False):
    PowerState = cmd.get_models(
        "PowerState",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    agentpool_exists = False
    instances = client.list(resource_group_name, cluster_name)
    for agentpool_profile in instances:
        if agentpool_profile.name.lower() == nodepool_name.lower():
            agentpool_exists = True
            break
    if not agentpool_exists:
        raise InvalidArgumentValueError(
            "Node pool {} doesnt exist, use 'aks nodepool list' to get current node pool list".format(nodepool_name))
    instance = client.get(resource_group_name, cluster_name, nodepool_name)
    power_state = PowerState(code="Running")
    instance.power_state = power_state
    headers = get_aks_custom_headers(aks_custom_headers)
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, nodepool_name, instance, headers=headers)


def aks_agentpool_delete(cmd,   # pylint: disable=unused-argument
                         client,
                         resource_group_name,
                         cluster_name,
                         nodepool_name,
                         ignore_pod_disruption_budget=None,
                         no_wait=False):
    agentpool_exists = False
    instances = client.list(resource_group_name, cluster_name)
    for agentpool_profile in instances:
        if agentpool_profile.name.lower() == nodepool_name.lower():
            agentpool_exists = True
            break

    if not agentpool_exists:
        raise CLIError("Node pool {} doesnt exist, "
                       "use 'aks nodepool list' to get current node pool list".format(nodepool_name))

    return sdk_no_wait(no_wait, client.begin_delete, resource_group_name, cluster_name, nodepool_name, ignore_pod_disruption_budget=ignore_pod_disruption_budget)


def aks_agentpool_operation_abort(cmd,   # pylint: disable=unused-argument
                                  client,
                                  resource_group_name,
                                  cluster_name,
                                  nodepool_name,
                                  aks_custom_headers=None,
                                  no_wait=False):
    PowerState = cmd.get_models(
        "PowerState",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="agent_pools",
    )

    agentpool_exists = False
    instances = client.list(resource_group_name, cluster_name)
    for agentpool_profile in instances:
        if agentpool_profile.name.lower() == nodepool_name.lower():
            agentpool_exists = True
            break
    if not agentpool_exists:
        raise InvalidArgumentValueError(
            "Node pool {} doesnt exist, use 'aks nodepool list' to get current node pool list".format(nodepool_name))
    instance = client.get(resource_group_name, cluster_name, nodepool_name)
    power_state = PowerState(code="Running")
    instance.power_state = power_state
    headers = get_aks_custom_headers(aks_custom_headers)
    return sdk_no_wait(no_wait, client.abort_latest_operation, resource_group_name, cluster_name, nodepool_name, headers=headers)


def aks_operation_abort(cmd,   # pylint: disable=unused-argument
                        client,
                        resource_group_name,
                        name,
                        aks_custom_headers=None,
                        no_wait=False):
    PowerState = cmd.get_models(
        "PowerState",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    instance = client.get(resource_group_name, name)
    power_state = PowerState(code="Running")
    if instance is None:
        raise InvalidArgumentValueError("Cluster {} doesnt exist, use 'aks list' to get current cluster list".format(name))
    instance.power_state = power_state
    headers = get_aks_custom_headers(aks_custom_headers)
    return sdk_no_wait(no_wait, client.abort_latest_operation, resource_group_name, name, headers=headers)


def aks_addon_list_available():
    available_addons = []
    for k, v in ADDONS.items():
        available_addons.append({
            "name": k,
            "description": ADDONS_DESCRIPTIONS[v]
        })
    return available_addons


# pylint: disable=unused-argument
def aks_addon_list(cmd, client, resource_group_name, name):
    mc = client.get(resource_group_name, name)
    current_addons = []
    os_type = 'Linux'

    for name, addon_key in ADDONS.items():
        # web_application_routing is a special case, the configuration is stored in a separate profile
        if name == "web_application_routing":
            enabled = (
                True
                if mc.ingress_profile and
                mc.ingress_profile.web_app_routing and
                mc.ingress_profile.web_app_routing.enabled
                else False
            )
        else:
            if name == "virtual-node":
                addon_key += os_type
            enabled = (
                True
                if mc.addon_profiles and
                addon_key in mc.addon_profiles and
                mc.addon_profiles[addon_key].enabled
                else False
            )
        current_addons.append({
            "name": name,
            "api_key": addon_key,
            "enabled": enabled
        })

    return current_addons


# pylint: disable=unused-argument
def aks_addon_show(cmd, client, resource_group_name, name, addon):
    mc = client.get(resource_group_name, name)
    addon_key = ADDONS[addon]

    # web_application_routing is a special case, the configuration is stored in a separate profile
    if addon == "web_application_routing":
        if not mc.ingress_profile and not mc.ingress_profile.web_app_routing and not mc.ingress_profile.web_app_routing.enabled:
            raise InvalidArgumentValueError(f'Addon "{addon}" is not enabled in this cluster.')
        return {
            "name": addon,
            "api_key": addon_key,
            "config": mc.ingress_profile.web_app_routing,
        }

    # normal addons
    if not mc.addon_profiles or addon_key not in mc.addon_profiles or not mc.addon_profiles[addon_key].enabled:
        raise InvalidArgumentValueError(f'Addon "{addon}" is not enabled in this cluster.')
    return {
        "name": addon,
        "api_key": addon_key,
        "config": mc.addon_profiles[addon_key].config,
        "identity": mc.addon_profiles[addon_key].identity
    }


def aks_addon_enable(cmd, client, resource_group_name, name, addon, workspace_resource_id=None,
                     subnet_name=None, appgw_name=None, appgw_subnet_prefix=None, appgw_subnet_cidr=None, appgw_id=None,
                     appgw_subnet_id=None,
                     appgw_watch_namespace=None, enable_sgxquotehelper=False, enable_secret_rotation=False, rotation_poll_interval=None,
                     no_wait=False, enable_msi_auth_for_monitoring=False,
                     dns_zone_resource_id=None):
    return enable_addons(cmd, client, resource_group_name, name, addon, workspace_resource_id=workspace_resource_id,
                         subnet_name=subnet_name, appgw_name=appgw_name, appgw_subnet_prefix=appgw_subnet_prefix,
                         appgw_subnet_cidr=appgw_subnet_cidr, appgw_id=appgw_id, appgw_subnet_id=appgw_subnet_id,
                         appgw_watch_namespace=appgw_watch_namespace, enable_sgxquotehelper=enable_sgxquotehelper,
                         enable_secret_rotation=enable_secret_rotation, rotation_poll_interval=rotation_poll_interval, no_wait=no_wait,
                         enable_msi_auth_for_monitoring=enable_msi_auth_for_monitoring,
                         dns_zone_resource_id=dns_zone_resource_id)


def aks_addon_disable(cmd, client, resource_group_name, name, addon, no_wait=False):
    return aks_disable_addons(cmd, client, resource_group_name, name, addon, no_wait)


def aks_addon_update(cmd, client, resource_group_name, name, addon, workspace_resource_id=None,
                     subnet_name=None, appgw_name=None, appgw_subnet_prefix=None, appgw_subnet_cidr=None, appgw_id=None,
                     appgw_subnet_id=None,
                     appgw_watch_namespace=None, enable_sgxquotehelper=False, enable_secret_rotation=False, rotation_poll_interval=None,
                     no_wait=False, enable_msi_auth_for_monitoring=False,
                     dns_zone_resource_id=None):
    instance = client.get(resource_group_name, name)
    addon_profiles = instance.addon_profiles

    if addon == "web_application_routing":
        if (instance.ingress_profile is None) or (instance.ingress_profile.web_app_routing is None) or not instance.ingress_profile.web_app_routing.enabled:
            raise InvalidArgumentValueError(f'Addon "{addon}" is not enabled in this cluster.')
    else:
        addon_key = ADDONS[addon]
        if not addon_profiles or addon_key not in addon_profiles or not addon_profiles[addon_key].enabled:
            raise InvalidArgumentValueError(f'Addon "{addon}" is not enabled in this cluster.')

    return enable_addons(cmd, client, resource_group_name, name, addon, check_enabled=False,
                         workspace_resource_id=workspace_resource_id,
                         subnet_name=subnet_name, appgw_name=appgw_name, appgw_subnet_prefix=appgw_subnet_prefix,
                         appgw_subnet_cidr=appgw_subnet_cidr, appgw_id=appgw_id, appgw_subnet_id=appgw_subnet_id,
                         appgw_watch_namespace=appgw_watch_namespace, enable_sgxquotehelper=enable_sgxquotehelper,
                         enable_secret_rotation=enable_secret_rotation, rotation_poll_interval=rotation_poll_interval, no_wait=no_wait,
                         enable_msi_auth_for_monitoring=enable_msi_auth_for_monitoring,
                         dns_zone_resource_id=dns_zone_resource_id)


def aks_disable_addons(cmd, client, resource_group_name, name, addons, no_wait=False):
    instance = client.get(resource_group_name, name)
    subscription_id = get_subscription_id(cmd.cli_ctx)

    try:
        if addons == "monitoring" and CONST_MONITORING_ADDON_NAME in instance.addon_profiles and \
                instance.addon_profiles[CONST_MONITORING_ADDON_NAME].enabled and \
                CONST_MONITORING_USING_AAD_MSI_AUTH in instance.addon_profiles[CONST_MONITORING_ADDON_NAME].config and \
                str(instance.addon_profiles[CONST_MONITORING_ADDON_NAME].config[CONST_MONITORING_USING_AAD_MSI_AUTH]).lower() == 'true':
            # remove the DCR association because otherwise the DCR can't be deleted
            ensure_container_insights_for_monitoring(
                cmd,
                instance.addon_profiles[CONST_MONITORING_ADDON_NAME],
                subscription_id,
                resource_group_name,
                name,
                instance.location,
                remove_monitoring=True,
                aad_route=True,
                create_dcr=False,
                create_dcra=True
            )
    except TypeError:
        pass

    instance = _update_addons(
        cmd,
        instance,
        subscription_id,
        resource_group_name,
        name,
        addons,
        enable=False,
        no_wait=no_wait
    )

    # send the managed cluster representation to update the addon profiles
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, name, instance)


def aks_enable_addons(cmd, client, resource_group_name, name, addons, workspace_resource_id=None,
                      subnet_name=None, appgw_name=None, appgw_subnet_prefix=None, appgw_subnet_cidr=None, appgw_id=None, appgw_subnet_id=None,
                      appgw_watch_namespace=None, enable_sgxquotehelper=False, enable_secret_rotation=False, rotation_poll_interval=None, no_wait=False, enable_msi_auth_for_monitoring=False,
                      dns_zone_resource_id=None):

    instance = client.get(resource_group_name, name)
    # this is overwritten by _update_addons(), so the value needs to be recorded here
    msi_auth = True if instance.service_principal_profile.client_id == "msi" else False

    subscription_id = get_subscription_id(cmd.cli_ctx)
    instance = _update_addons(cmd, instance, subscription_id, resource_group_name, name, addons, enable=True,
                              workspace_resource_id=workspace_resource_id, enable_msi_auth_for_monitoring=enable_msi_auth_for_monitoring, subnet_name=subnet_name,
                              appgw_name=appgw_name, appgw_subnet_prefix=appgw_subnet_prefix, appgw_subnet_cidr=appgw_subnet_cidr, appgw_id=appgw_id, appgw_subnet_id=appgw_subnet_id, appgw_watch_namespace=appgw_watch_namespace,
                              enable_sgxquotehelper=enable_sgxquotehelper, enable_secret_rotation=enable_secret_rotation, rotation_poll_interval=rotation_poll_interval, no_wait=no_wait,
                              dns_zone_resource_id=dns_zone_resource_id)

    if CONST_MONITORING_ADDON_NAME in instance.addon_profiles and instance.addon_profiles[CONST_MONITORING_ADDON_NAME].enabled:
        if CONST_MONITORING_USING_AAD_MSI_AUTH in instance.addon_profiles[CONST_MONITORING_ADDON_NAME].config and \
                str(instance.addon_profiles[CONST_MONITORING_ADDON_NAME].config[CONST_MONITORING_USING_AAD_MSI_AUTH]).lower() == 'true':
            if not msi_auth:
                raise ArgumentUsageError(
                    "--enable-msi-auth-for-monitoring can not be used on clusters with service principal auth.")
            else:
                # create a Data Collection Rule (DCR) and associate it with the cluster
                ensure_container_insights_for_monitoring(
                    cmd, instance.addon_profiles[CONST_MONITORING_ADDON_NAME], subscription_id, resource_group_name, name, instance.location, aad_route=True, create_dcr=True, create_dcra=True)
        else:
            # monitoring addon will use legacy path
            ensure_container_insights_for_monitoring(
                cmd, instance.addon_profiles[CONST_MONITORING_ADDON_NAME], subscription_id, resource_group_name, name, instance.location, aad_route=False)

    monitoring = CONST_MONITORING_ADDON_NAME in instance.addon_profiles and instance.addon_profiles[
        CONST_MONITORING_ADDON_NAME].enabled
    ingress_appgw_addon_enabled = CONST_INGRESS_APPGW_ADDON_NAME in instance.addon_profiles and instance.addon_profiles[
        CONST_INGRESS_APPGW_ADDON_NAME].enabled

    os_type = 'Linux'
    enable_virtual_node = False
    if CONST_VIRTUAL_NODE_ADDON_NAME + os_type in instance.addon_profiles:
        enable_virtual_node = True

    need_post_creation_role_assignment = monitoring or ingress_appgw_addon_enabled or enable_virtual_node
    if need_post_creation_role_assignment:
        # adding a wait here since we rely on the result for role assignment
        result = LongRunningOperation(cmd.cli_ctx)(
            client.begin_create_or_update(resource_group_name, name, instance))
        cloud_name = cmd.cli_ctx.cloud.name
        # mdm metrics supported only in Azure Public cloud so add the role assignment only in this cloud
        if monitoring and cloud_name.lower() == 'azurecloud':
            from msrestazure.tools import resource_id
            cluster_resource_id = resource_id(
                subscription=subscription_id,
                resource_group=resource_group_name,
                namespace='Microsoft.ContainerService', type='managedClusters',
                name=name
            )
            add_monitoring_role_assignment(result, cluster_resource_id, cmd)
        if ingress_appgw_addon_enabled:
            add_ingress_appgw_addon_role_assignment(result, cmd)
        if enable_virtual_node:
            # All agent pool will reside in the same vnet, we will grant vnet level Contributor role
            # in later function, so using a random agent pool here is OK
            random_agent_pool = result.agent_pool_profiles[0]
            if random_agent_pool.vnet_subnet_id != "":
                add_virtual_node_role_assignment(
                    cmd, result, random_agent_pool.vnet_subnet_id)
            # Else, the cluster is not using custom VNet, the permission is already granted in AKS RP,
            # we don't need to handle it in client side in this case.

    else:
        result = sdk_no_wait(no_wait, client.begin_create_or_update,
                             resource_group_name, name, instance)
    return result


def aks_rotate_certs(cmd, client, resource_group_name, name, no_wait=True):     # pylint: disable=unused-argument
    return sdk_no_wait(no_wait, client.begin_rotate_cluster_certificates, resource_group_name, name)


def _update_addons(cmd,  # pylint: disable=too-many-branches,too-many-statements
                   instance,
                   subscription_id,
                   resource_group_name,
                   name,
                   addons,
                   enable,
                   workspace_resource_id=None,
                   enable_msi_auth_for_monitoring=False,
                   subnet_name=None,
                   appgw_name=None,
                   appgw_subnet_prefix=None,
                   appgw_subnet_cidr=None,
                   appgw_id=None,
                   appgw_subnet_id=None,
                   appgw_watch_namespace=None,
                   enable_sgxquotehelper=False,
                   enable_secret_rotation=False,
                   disable_secret_rotation=False,
                   rotation_poll_interval=None,
                   dns_zone_resource_id=None,
                   no_wait=False):  # pylint: disable=unused-argument
    ManagedClusterAddonProfile = cmd.get_models(
        "ManagedClusterAddonProfile",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )
    ManagedClusterIngressProfile = cmd.get_models(
        "ManagedClusterIngressProfile",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )
    ManagedClusterIngressProfileWebAppRouting = cmd.get_models(
        "ManagedClusterIngressProfileWebAppRouting",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    # parse the comma-separated addons argument
    addon_args = addons.split(',')

    addon_profiles = instance.addon_profiles or {}

    os_type = 'Linux'

    # for each addons argument
    for addon_arg in addon_args:
        if addon_arg == "web_application_routing":
            # web app routing settings are in ingress profile, not addon profile, so deal
            # with it separately
            if instance.ingress_profile is None:
                instance.ingress_profile = ManagedClusterIngressProfile()
            if instance.ingress_profile.web_app_routing is None:
                instance.ingress_profile.web_app_routing = ManagedClusterIngressProfileWebAppRouting()
            instance.ingress_profile.web_app_routing.enabled = enable

            if dns_zone_resource_id is not None:
                instance.ingress_profile.web_app_routing.dns_zone_resource_id = dns_zone_resource_id
            continue

        if addon_arg not in ADDONS:
            raise CLIError("Invalid addon name: {}.".format(addon_arg))
        addon = ADDONS[addon_arg]
        if addon == CONST_VIRTUAL_NODE_ADDON_NAME:
            # only linux is supported for now, in the future this will be a user flag
            addon += os_type

        # honor addon names defined in Azure CLI
        for key in list(addon_profiles):
            if key.lower() == addon.lower() and key != addon:
                addon_profiles[addon] = addon_profiles.pop(key)

        if enable:
            # add new addons or update existing ones and enable them
            addon_profile = addon_profiles.get(
                addon, ManagedClusterAddonProfile(enabled=False))
            # special config handling for certain addons
            if addon == CONST_MONITORING_ADDON_NAME:
                logAnalyticsConstName = CONST_MONITORING_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID
                if addon_profile.enabled:
                    raise CLIError('The monitoring addon is already enabled for this managed cluster.\n'
                                   'To change monitoring configuration, run "az aks disable-addons -a monitoring"'
                                   'before enabling it again.')
                if not workspace_resource_id:
                    workspace_resource_id = ensure_default_log_analytics_workspace_for_monitoring(
                        cmd,
                        subscription_id,
                        resource_group_name)
                workspace_resource_id = sanitize_loganalytics_ws_resource_id(
                    workspace_resource_id)

                addon_profile.config = {
                    logAnalyticsConstName: workspace_resource_id}
                addon_profile.config[CONST_MONITORING_USING_AAD_MSI_AUTH] = enable_msi_auth_for_monitoring
            elif addon == (CONST_VIRTUAL_NODE_ADDON_NAME + os_type):
                if addon_profile.enabled:
                    raise CLIError('The virtual-node addon is already enabled for this managed cluster.\n'
                                   'To change virtual-node configuration, run '
                                   '"az aks disable-addons -a virtual-node -g {resource_group_name}" '
                                   'before enabling it again.')
                if not subnet_name:
                    raise CLIError(
                        'The aci-connector addon requires setting a subnet name.')
                addon_profile.config = {
                    CONST_VIRTUAL_NODE_SUBNET_NAME: subnet_name}
            elif addon == CONST_INGRESS_APPGW_ADDON_NAME:
                if addon_profile.enabled:
                    raise CLIError('The ingress-appgw addon is already enabled for this managed cluster.\n'
                                   'To change ingress-appgw configuration, run '
                                   f'"az aks disable-addons -a ingress-appgw -n {name} -g {resource_group_name}" '
                                   'before enabling it again.')
                addon_profile = ManagedClusterAddonProfile(
                    enabled=True, config={})
                if appgw_name is not None:
                    addon_profile.config[CONST_INGRESS_APPGW_APPLICATION_GATEWAY_NAME] = appgw_name
                if appgw_subnet_prefix is not None:
                    addon_profile.config[CONST_INGRESS_APPGW_SUBNET_CIDR] = appgw_subnet_prefix
                if appgw_subnet_cidr is not None:
                    addon_profile.config[CONST_INGRESS_APPGW_SUBNET_CIDR] = appgw_subnet_cidr
                if appgw_id is not None:
                    addon_profile.config[CONST_INGRESS_APPGW_APPLICATION_GATEWAY_ID] = appgw_id
                if appgw_subnet_id is not None:
                    addon_profile.config[CONST_INGRESS_APPGW_SUBNET_ID] = appgw_subnet_id
                if appgw_watch_namespace is not None:
                    addon_profile.config[CONST_INGRESS_APPGW_WATCH_NAMESPACE] = appgw_watch_namespace
            elif addon == CONST_OPEN_SERVICE_MESH_ADDON_NAME:
                if addon_profile.enabled:
                    raise CLIError('The open-service-mesh addon is already enabled for this managed cluster.\n'
                                   'To change open-service-mesh configuration, run '
                                   f'"az aks disable-addons -a open-service-mesh -n {name} -g {resource_group_name}" '
                                   'before enabling it again.')
                addon_profile = ManagedClusterAddonProfile(
                    enabled=True, config={})
            elif addon == CONST_CONFCOM_ADDON_NAME:
                if addon_profile.enabled:
                    raise CLIError('The confcom addon is already enabled for this managed cluster.\n'
                                   'To change confcom configuration, run '
                                   f'"az aks disable-addons -a confcom -n {name} -g {resource_group_name}" '
                                   'before enabling it again.')
                addon_profile = ManagedClusterAddonProfile(
                    enabled=True, config={CONST_ACC_SGX_QUOTE_HELPER_ENABLED: "false"})
                if enable_sgxquotehelper:
                    addon_profile.config[CONST_ACC_SGX_QUOTE_HELPER_ENABLED] = "true"
            elif addon == CONST_AZURE_KEYVAULT_SECRETS_PROVIDER_ADDON_NAME:
                if addon_profile.enabled:
                    raise CLIError('The azure-keyvault-secrets-provider addon is already enabled for this managed cluster.\n'
                                   'To change azure-keyvault-secrets-provider configuration, run '
                                   f'"az aks disable-addons -a azure-keyvault-secrets-provider -n {name} -g {resource_group_name}" '
                                   'before enabling it again.')
                addon_profile = ManagedClusterAddonProfile(
                    enabled=True, config={CONST_SECRET_ROTATION_ENABLED: "false", CONST_ROTATION_POLL_INTERVAL: "2m"})
                if enable_secret_rotation:
                    addon_profile.config[CONST_SECRET_ROTATION_ENABLED] = "true"
                if disable_secret_rotation:
                    addon_profile.config[CONST_SECRET_ROTATION_ENABLED] = "false"
                if rotation_poll_interval is not None:
                    addon_profile.config[CONST_ROTATION_POLL_INTERVAL] = rotation_poll_interval
                addon_profiles[CONST_AZURE_KEYVAULT_SECRETS_PROVIDER_ADDON_NAME] = addon_profile
            addon_profiles[addon] = addon_profile
        else:
            if addon not in addon_profiles:
                if addon == CONST_KUBE_DASHBOARD_ADDON_NAME:
                    addon_profiles[addon] = ManagedClusterAddonProfile(
                        enabled=False)
                else:
                    raise CLIError(
                        "The addon {} is not installed.".format(addon))
            addon_profiles[addon].config = None
        addon_profiles[addon].enabled = enable

    instance.addon_profiles = addon_profiles

    # null out the SP profile because otherwise validation complains
    instance.service_principal_profile = None

    return instance


def aks_get_versions(cmd, client, location):    # pylint: disable=unused-argument
    return client.list_orchestrators(location, resource_type='managedClusters')


def aks_get_os_options(cmd, client, location):    # pylint: disable=unused-argument
    return client.get_os_options(location, resource_type='managedClusters')


def get_aks_custom_headers(aks_custom_headers=None):
    headers = {}
    if aks_custom_headers is not None:
        if aks_custom_headers != "":
            for pair in aks_custom_headers.split(','):
                parts = pair.split('=')
                if len(parts) != 2:
                    raise CLIError('custom headers format is incorrect')
                headers[parts[0]] = parts[1]
    return headers


def aks_draft_create(destination='.',
                     app=None,
                     language=None,
                     create_config=None,
                     dockerfile_only=None,
                     deployment_only=None,
                     path=None):
    aks_draft_cmd_create(destination, app, language, create_config, dockerfile_only, deployment_only, path)


def aks_draft_setup_gh(app=None,
                       subscription_id=None,
                       resource_group=None,
                       provider="azure",
                       gh_repo=None,
                       path=None):
    aks_draft_cmd_setup_gh(app, subscription_id, resource_group, provider, gh_repo, path)


def aks_draft_generate_workflow(cluster_name=None,
                                registry_name=None,
                                container_name=None,
                                resource_group=None,
                                destination=None,
                                branch=None,
                                path=None):
    aks_draft_cmd_generate_workflow(cluster_name, registry_name, container_name,
                                    resource_group, destination, branch, path)


def aks_draft_up(app=None,
                 subscription_id=None,
                 resource_group=None,
                 provider="azure",
                 gh_repo=None,
                 cluster_name=None,
                 registry_name=None,
                 container_name=None,
                 destination=None,
                 branch=None,
                 path=None):
    aks_draft_cmd_up(app, subscription_id, resource_group, provider, gh_repo,
                     cluster_name, registry_name, container_name, destination, branch, path)


def aks_draft_update(host=None, certificate=None, destination=None, path=None):
    aks_draft_cmd_update(host, certificate, destination, path)


def aks_kollect(cmd,    # pylint: disable=too-many-statements,too-many-locals
                client,
                resource_group_name,
                name,
                storage_account=None,
                sas_token=None,
                container_logs=None,
                kube_objects=None,
                node_logs=None,
                node_logs_windows=None):
    aks_kollect_cmd(cmd, client, resource_group_name, name, storage_account, sas_token,
                    container_logs, kube_objects, node_logs, node_logs_windows)


def aks_kanalyze(client, resource_group_name, name):
    aks_kanalyze_cmd(client, resource_group_name, name)


def aks_pod_identity_add(cmd, client, resource_group_name, cluster_name,
                         identity_name, identity_namespace, identity_resource_id,
                         binding_selector=None,
                         no_wait=False):  # pylint: disable=unused-argument
    ManagedClusterPodIdentity = cmd.get_models(
        "ManagedClusterPodIdentity",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )
    UserAssignedIdentity = cmd.get_models(
        "UserAssignedIdentity",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    instance = client.get(resource_group_name, cluster_name)
    _ensure_pod_identity_addon_is_enabled(instance)

    user_assigned_identity = _get_user_assigned_identity(
        cmd.cli_ctx, identity_resource_id)
    _ensure_managed_identity_operator_permission(
        cmd.cli_ctx, instance, user_assigned_identity.id)

    pod_identities = []
    if instance.pod_identity_profile.user_assigned_identities:
        pod_identities = instance.pod_identity_profile.user_assigned_identities
    pod_identity = ManagedClusterPodIdentity(
        name=identity_name,
        namespace=identity_namespace,
        identity=UserAssignedIdentity(
            resource_id=user_assigned_identity.id,
            client_id=user_assigned_identity.client_id,
            object_id=user_assigned_identity.principal_id,
        )
    )
    if binding_selector is not None:
        pod_identity.binding_selector = binding_selector
    pod_identities.append(pod_identity)

    from azext_aks_preview.managed_cluster_decorator import AKSPreviewManagedClusterModels
    # store all the models used by pod identity
    pod_identity_models = AKSPreviewManagedClusterModels(
        cmd, CUSTOM_MGMT_AKS_PREVIEW).pod_identity_models
    _update_addon_pod_identity(
        instance, enable=True,
        pod_identities=pod_identities,
        pod_identity_exceptions=instance.pod_identity_profile.user_assigned_identity_exceptions,
        models=pod_identity_models
    )

    # send the managed cluster represeentation to update the pod identity addon
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, instance)


def aks_pod_identity_delete(cmd, client, resource_group_name, cluster_name,
                            identity_name, identity_namespace,
                            no_wait=False):  # pylint: disable=unused-argument
    instance = client.get(resource_group_name, cluster_name)
    _ensure_pod_identity_addon_is_enabled(instance)

    pod_identities = []
    if instance.pod_identity_profile.user_assigned_identities:
        for pod_identity in instance.pod_identity_profile.user_assigned_identities:
            if pod_identity.name == identity_name and pod_identity.namespace == identity_namespace:
                # to remove
                continue
            pod_identities.append(pod_identity)

    from azext_aks_preview.managed_cluster_decorator import AKSPreviewManagedClusterModels
    # store all the models used by pod identity
    pod_identity_models = AKSPreviewManagedClusterModels(
        cmd, CUSTOM_MGMT_AKS_PREVIEW).pod_identity_models
    _update_addon_pod_identity(
        instance, enable=True,
        pod_identities=pod_identities,
        pod_identity_exceptions=instance.pod_identity_profile.user_assigned_identity_exceptions,
        models=pod_identity_models
    )

    # send the managed cluster represeentation to update the pod identity addon
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, instance)


def aks_pod_identity_list(cmd, client, resource_group_name, cluster_name):  # pylint: disable=unused-argument
    instance = client.get(resource_group_name, cluster_name)
    return _remove_nulls([instance])[0]


def aks_pod_identity_exception_add(cmd, client, resource_group_name, cluster_name,
                                   exc_name, exc_namespace, pod_labels, no_wait=False):  # pylint: disable=unused-argument
    ManagedClusterPodIdentityException = cmd.get_models(
        "ManagedClusterPodIdentityException",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    instance = client.get(resource_group_name, cluster_name)
    _ensure_pod_identity_addon_is_enabled(instance)

    pod_identity_exceptions = []
    if instance.pod_identity_profile.user_assigned_identity_exceptions:
        pod_identity_exceptions = instance.pod_identity_profile.user_assigned_identity_exceptions
    exc = ManagedClusterPodIdentityException(
        name=exc_name, namespace=exc_namespace, pod_labels=pod_labels)
    pod_identity_exceptions.append(exc)

    from azext_aks_preview.managed_cluster_decorator import AKSPreviewManagedClusterModels
    # store all the models used by pod identity
    pod_identity_models = AKSPreviewManagedClusterModels(
        cmd, CUSTOM_MGMT_AKS_PREVIEW).pod_identity_models
    _update_addon_pod_identity(
        instance, enable=True,
        pod_identities=instance.pod_identity_profile.user_assigned_identities,
        pod_identity_exceptions=pod_identity_exceptions,
        models=pod_identity_models
    )

    # send the managed cluster represeentation to update the pod identity addon
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, instance)


def aks_pod_identity_exception_delete(cmd, client, resource_group_name, cluster_name,
                                      exc_name, exc_namespace, no_wait=False):  # pylint: disable=unused-argument
    instance = client.get(resource_group_name, cluster_name)
    _ensure_pod_identity_addon_is_enabled(instance)

    pod_identity_exceptions = []
    if instance.pod_identity_profile.user_assigned_identity_exceptions:
        for exc in instance.pod_identity_profile.user_assigned_identity_exceptions:
            if exc.name == exc_name and exc.namespace == exc_namespace:
                # to remove
                continue
            pod_identity_exceptions.append(exc)

    from azext_aks_preview.managed_cluster_decorator import AKSPreviewManagedClusterModels
    # store all the models used by pod identity
    pod_identity_models = AKSPreviewManagedClusterModels(
        cmd, CUSTOM_MGMT_AKS_PREVIEW).pod_identity_models
    _update_addon_pod_identity(
        instance, enable=True,
        pod_identities=instance.pod_identity_profile.user_assigned_identities,
        pod_identity_exceptions=pod_identity_exceptions,
        models=pod_identity_models
    )

    # send the managed cluster represeentation to update the pod identity addon
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, instance)


def aks_pod_identity_exception_update(cmd, client, resource_group_name, cluster_name,
                                      exc_name, exc_namespace, pod_labels, no_wait=False):  # pylint: disable=unused-argument
    ManagedClusterPodIdentityException = cmd.get_models(
        "ManagedClusterPodIdentityException",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    instance = client.get(resource_group_name, cluster_name)
    _ensure_pod_identity_addon_is_enabled(instance)

    found_target = False
    updated_exc = ManagedClusterPodIdentityException(
        name=exc_name, namespace=exc_namespace, pod_labels=pod_labels)
    pod_identity_exceptions = []
    if instance.pod_identity_profile.user_assigned_identity_exceptions:
        for exc in instance.pod_identity_profile.user_assigned_identity_exceptions:
            if exc.name == exc_name and exc.namespace == exc_namespace:
                found_target = True
                pod_identity_exceptions.append(updated_exc)
            else:
                pod_identity_exceptions.append(exc)

    if not found_target:
        raise CLIError(
            'pod identity exception {}/{} not found'.format(exc_namespace, exc_name))

    from azext_aks_preview.managed_cluster_decorator import AKSPreviewManagedClusterModels
    # store all the models used by pod identity
    pod_identity_models = AKSPreviewManagedClusterModels(
        cmd, CUSTOM_MGMT_AKS_PREVIEW).pod_identity_models
    _update_addon_pod_identity(
        instance, enable=True,
        pod_identities=instance.pod_identity_profile.user_assigned_identities,
        pod_identity_exceptions=pod_identity_exceptions,
        models=pod_identity_models
    )

    # send the managed cluster represeentation to update the pod identity addon
    return sdk_no_wait(no_wait, client.begin_create_or_update, resource_group_name, cluster_name, instance)


def aks_pod_identity_exception_list(cmd, client, resource_group_name, cluster_name):
    instance = client.get(resource_group_name, cluster_name)
    return _remove_nulls([instance])[0]


def aks_egress_endpoints_list(cmd, client, resource_group_name, name):   # pylint: disable=unused-argument
    return client.list_outbound_network_dependencies_endpoints(resource_group_name, name)


def aks_snapshot_create(cmd,    # pylint: disable=too-many-locals,too-many-statements,too-many-branches
                        client,
                        resource_group_name,
                        name,
                        cluster_id,
                        location=None,
                        tags=None,
                        aks_custom_headers=None,
                        no_wait=False):
    ManagedClusterSnapshot = cmd.get_models(
        "ManagedClusterSnapshot",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_cluster_snapshots",
    )
    CreationData = cmd.get_models(
        "CreationData",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    rg_location = get_rg_location(cmd.cli_ctx, resource_group_name)
    if location is None:
        location = rg_location

    creationData = CreationData(
        source_resource_id=cluster_id
    )

    snapshot = ManagedClusterSnapshot(
        name=name,
        tags=tags,
        location=location,
        creation_data=creationData,
        snapshot_type="ManagedCluster",
    )

    headers = get_aks_custom_headers(aks_custom_headers)
    return client.create_or_update(resource_group_name, name, snapshot, headers=headers)


def aks_snapshot_show(cmd, client, resource_group_name, name):   # pylint: disable=unused-argument
    snapshot = client.get(resource_group_name, name)
    return snapshot


def aks_snapshot_delete(cmd,    # pylint: disable=unused-argument
                        client,
                        resource_group_name,
                        name,
                        no_wait=False,
                        yes=False):

    from knack.prompting import prompt_y_n
    msg = 'This will delete the cluster snapshot "{}" in resource group "{}", Are you sure?'.format(
        name, resource_group_name)
    if not yes and not prompt_y_n(msg, default="n"):
        return None

    return client.delete(resource_group_name, name)


def aks_snapshot_list(cmd, client, resource_group_name=None):  # pylint: disable=unused-argument
    if resource_group_name is None or resource_group_name == '':
        return client.list()

    return client.list_by_resource_group(resource_group_name)


def aks_nodepool_snapshot_create(cmd,    # pylint: disable=too-many-locals,too-many-statements,too-many-branches
                                 client,
                                 resource_group_name,
                                 snapshot_name,
                                 nodepool_id,
                                 location=None,
                                 tags=None,
                                 aks_custom_headers=None,
                                 no_wait=False):
    Snapshot = cmd.get_models(
        "Snapshot",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="snapshots",
    )
    CreationData = cmd.get_models(
        "CreationData",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="managed_clusters",
    )

    rg_location = get_rg_location(cmd.cli_ctx, resource_group_name)
    if location is None:
        location = rg_location

    creationData = CreationData(
        source_resource_id=nodepool_id
    )

    snapshot = Snapshot(
        name=snapshot_name,
        tags=tags,
        location=location,
        creation_data=creationData
    )

    headers = get_aks_custom_headers(aks_custom_headers)
    return client.create_or_update(resource_group_name, snapshot_name, snapshot, headers=headers)


def aks_nodepool_snapshot_show(cmd, client, resource_group_name, snapshot_name):   # pylint: disable=unused-argument
    snapshot = client.get(resource_group_name, snapshot_name)
    return snapshot


def aks_nodepool_snapshot_delete(cmd,    # pylint: disable=unused-argument
                                 client,
                                 resource_group_name,
                                 snapshot_name,
                                 no_wait=False,
                                 yes=False):

    from knack.prompting import prompt_y_n
    msg = 'This will delete the nodepool snapshot "{}" in resource group "{}", Are you sure?'.format(
        snapshot_name, resource_group_name)
    if not yes and not prompt_y_n(msg, default="n"):
        return None

    return client.delete(resource_group_name, snapshot_name)


def aks_nodepool_snapshot_list(cmd, client, resource_group_name=None):  # pylint: disable=unused-argument
    if resource_group_name is None or resource_group_name == '':
        return client.list()

    return client.list_by_resource_group(resource_group_name)


def aks_trustedaccess_role_list(cmd, client, location):  # pylint: disable=unused-argument
    return client.list(location)


def aks_trustedaccess_role_binding_list(cmd, client, resource_group_name, cluster_name):   # pylint: disable=unused-argument
    return client.list(resource_group_name, cluster_name)


def aks_trustedaccess_role_binding_get(cmd, client, resource_group_name, cluster_name, role_binding_name):
    return client.get(resource_group_name, cluster_name, role_binding_name)


def aks_trustedaccess_role_binding_create(cmd, client, resource_group_name, cluster_name, role_binding_name,
                                          source_resource_id, roles):
    TrustedAccessRoleBinding = cmd.get_models(
        "TrustedAccessRoleBinding",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="trusted_access_role_bindings",
    )
    roleList = roles.split(',')
    roleBinding = TrustedAccessRoleBinding(source_resource_id=source_resource_id, roles=roleList)
    return client.create_or_update(resource_group_name, cluster_name, role_binding_name, roleBinding)


def aks_trustedaccess_role_binding_update(cmd, client, resource_group_name, cluster_name, role_binding_name, roles):
    TrustedAccessRoleBinding = cmd.get_models(
        "TrustedAccessRoleBinding",
        resource_type=CUSTOM_MGMT_AKS_PREVIEW,
        operation_group="trusted_access_role_bindings",
    )
    existedBinding = client.get(resource_group_name, cluster_name, role_binding_name)

    roleList = roles.split(',')
    roleBinding = TrustedAccessRoleBinding(source_resource_id=existedBinding.source_resource_id, roles=roleList)
    return client.create_or_update(resource_group_name, cluster_name, role_binding_name, roleBinding)


def aks_trustedaccess_role_binding_delete(cmd, client, resource_group_name, cluster_name, role_binding_name):
    return client.delete(resource_group_name, cluster_name, role_binding_name)
