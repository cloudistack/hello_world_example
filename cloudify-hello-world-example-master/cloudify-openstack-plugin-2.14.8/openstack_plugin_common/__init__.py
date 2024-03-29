#########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

from functools import wraps, partial
import json
import os
import sys
import logging
import copy

from IPy import IP
from keystoneauth1 import loading, session
import cinderclient.client as cinder_client
import cinderclient.exceptions as cinder_exceptions
import keystoneclient.v3.client as keystone_client
import keystoneclient.exceptions as keystone_exceptions
import neutronclient.v2_0.client as neutron_client
import neutronclient.common.exceptions as neutron_exceptions
import novaclient.client as nova_client
import novaclient.exceptions as nova_exceptions
import glanceclient.client as glance_client
import glanceclient.exc as glance_exceptions

import cloudify
from cloudify import context, ctx
from cloudify.exceptions import NonRecoverableError, RecoverableError

INFINITE_RESOURCE_QUOTA = -1

# properties
USE_EXTERNAL_RESOURCE_PROPERTY = 'use_external_resource'
CREATE_IF_MISSING_PROPERTY = 'create_if_missing'
CONFIG_PROPERTY = 'openstack_config'

# runtime properties
OPENSTACK_AZ_PROPERTY = 'availability_zone'
OPENSTACK_ID_PROPERTY = 'external_id'  # resource's openstack id
OPENSTACK_TYPE_PROPERTY = 'external_type'  # resource's openstack type
OPENSTACK_NAME_PROPERTY = 'external_name'  # resource's openstack name
OPENSTACK_RESOURCE_PROPERTY = 'external_resource'  # resource's parameters
CONDITIONALLY_CREATED = 'conditionally_created'  # resource was
# conditionally created
CONFIG_RUNTIME_PROPERTY = CONFIG_PROPERTY   # openstack configuration

# operation inputs
CONFIG_INPUT = CONFIG_PROPERTY

# runtime properties which all types use
COMMON_RUNTIME_PROPERTIES_KEYS = [OPENSTACK_ID_PROPERTY,
                                  OPENSTACK_TYPE_PROPERTY,
                                  OPENSTACK_NAME_PROPERTY,
                                  OPENSTACK_RESOURCE_PROPERTY,
                                  CONDITIONALLY_CREATED]

MISSING_RESOURCE_MESSAGE = "Couldn't find a resource of " \
                           "type {0} with the name or id {1}"

AUTH_PARAM_INSECURE = 'insecure'
AUTH_PARM_CA_CERT = 'ca_cert'

KEY_USE_CFY_LOGGER = 'use_cfy_logger'
KEY_GROUPS = 'groups'
KEY_LOGGERS = 'loggers'

DEFAULT_LOGGING_CONFIG = {
    KEY_USE_CFY_LOGGER: True,
    KEY_GROUPS: {
        'nova': logging.DEBUG,
        'neutron': logging.DEBUG,
        'cinder': logging.DEBUG,
        'keystone': logging.DEBUG,
        'glance': logging.DEBUG
    },
    KEY_LOGGERS: {
        'keystoneauth.session': logging.DEBUG
    }
}

LOGGING_GROUPS = {
    'nova': ['novaclient.client', 'novaclient.v2.client'],
    'neutron': ['neutronclient.client', 'neutronclient.v2_0.client'],
    'cinder': ['cinderclient.client', 'cinderclient.v1.client',
               'cinderclient.v2.client', 'cinderclient.v3.client'],
    'keystone': ['keystoneclient.client', 'keystoneclient.v2_0.client',
                 'keystoneclient.v3.client'],
    'glance': ['glanceclient.client', 'glanceclient.v1.client',
               'glanceclient.v2.client']
}


# TODO: Move this to cloudify-plugins-common (code freeze currently
# in effect).
class CloudifyCtxLogHandler(logging.Handler):
    """
    A logging handler for Cloudify.
    A logger attached to this handler will result in logging being passed
    through to the Cloudify logger.
    """
    def __init__(self, ctx):
        """
        Constructor.
        :param ctx: current Cloudify context
        """
        logging.Handler.__init__(self)
        self.ctx = ctx

    def emit(self, record):
        """
        Callback to emit a log record.
        :param record: log record to write
        :type record: logging.LogRecord
        """
        message = self.format(record)
        self.ctx.logger.log(record.levelno, message)


class ProviderContext(object):

    def __init__(self, provider_context):
        self._provider_context = provider_context or {}
        self._resources = self._provider_context.get('resources', {})

    @property
    def agents_keypair(self):
        return self._resources.get('agents_keypair')

    @property
    def agents_security_group(self):
        return self._resources.get('agents_security_group')

    @property
    def ext_network(self):
        return self._resources.get('ext_network')

    @property
    def floating_ip(self):
        return self._resources.get('floating_ip')

    @property
    def int_network(self):
        return self._resources.get('int_network')

    @property
    def management_keypair(self):
        return self._resources.get('management_keypair')

    @property
    def management_security_group(self):
        return self._resources.get('management_security_group')

    @property
    def management_server(self):
        return self._resources.get('management_server')

    @property
    def router(self):
        return self._resources.get('router')

    @property
    def subnet(self):
        return self._resources.get('subnet')

    def __repr__(self):
        info = json.dumps(self._provider_context)
        return '<' + self.__class__.__name__ + ' ' + info + '>'


def provider(ctx):
    return ProviderContext(ctx.provider_context)


def assign_payload_as_runtime_properties(ctx, resource_name, payload={}):
    """
    In general Openstack API objects have create, update, and delete
    functions. Each function normally receives a payload that describes
    the desired configuration of the object.
    This makes sure to store that configuration in the runtime
    properties and cleans any potentially sensitive data.

    :param ctx: The Cloudify NodeInstanceContext
    :param resource_name: A string describing the resource.
    :param payload: The payload.
    :return:
    """

    # Avoid failing if a developer inadvertently passes a
    # non-NodeInstanceContext
    if getattr(ctx, 'instance'):
        if resource_name not in ctx.instance.runtime_properties.keys():
            ctx.instance.runtime_properties[resource_name] = {}
        for key, value in payload.items():
            if key != 'user_data' and key != 'adminPass':
                ctx.instance.runtime_properties[resource_name][key] = value


def get_relationships_by_relationship_type(ctx, type_name):
    """
    Get cloudify relationships by relationship type.
    Follows the inheritance tree.

    :param ctx: Cloudify NodeInstanceContext
    :param type_name: desired relationship type derived
    from cloudify.relationships.depends_on.
    :return: list of RelationshipSubjectContext
    """

    return [rel for rel in ctx.instance.relationships if
            type_name in rel.type_hierarchy]


def get_attribute_of_connected_nodes_by_relationship_type(ctx,
                                                          type_name,
                                                          attribute_name):
    """
    Returns a list of OPENSTACK_ID_PROPERTY from a list of
    Cloudify RelationshipSubjectContext.

    :param ctx: Cloudify NodeInstanceContext
    :param type_name: desired relationship type derived
    from cloudify.relationships.depends_on.
    :param attribute_name: usually either
    OPENSTACK_NAME_PROPERTY or OPENSTACK_ID_PROPERTY
    :return:
    """

    return [rel.target.instance.runtime_properties[attribute_name]
            for rel in get_relationships_by_relationship_type(ctx, type_name)]


def get_relationships_by_openstack_type(ctx, type_name):
    return [rel for rel in ctx.instance.relationships
            if rel.target.instance.runtime_properties.get(
                OPENSTACK_TYPE_PROPERTY) == type_name]


def get_connected_nodes_by_openstack_type(ctx, type_name):
    return [rel.target.node
            for rel in get_relationships_by_openstack_type(ctx, type_name)]


def get_openstack_ids_of_connected_nodes_by_openstack_type(ctx, type_name):
    return [rel.target.instance.runtime_properties[OPENSTACK_ID_PROPERTY]
            for rel in get_relationships_by_openstack_type(ctx, type_name)
            ]


def get_openstack_names_of_connected_nodes_by_openstack_type(ctx, type_name):
    return [rel.target.instance.runtime_properties[OPENSTACK_NAME_PROPERTY]
            for rel in get_relationships_by_openstack_type(ctx, type_name)
            ]


def get_single_connected_node_by_openstack_type(
        ctx, type_name, if_exists=False):
    nodes = get_connected_nodes_by_openstack_type(ctx, type_name)
    check = len(nodes) > 1 if if_exists else len(nodes) != 1
    if check:
        raise NonRecoverableError(
            'Expected {0} one {1} node. got {2}'.format(
                'at most' if if_exists else 'exactly', type_name, len(nodes)))
    return nodes[0] if nodes else None


def get_openstack_id_of_single_connected_node_by_openstack_type(
        ctx, type_name, if_exists=False):
    ids = get_openstack_ids_of_connected_nodes_by_openstack_type(ctx,
                                                                 type_name)
    check = len(ids) > 1 if if_exists else len(ids) != 1
    if check:
        raise NonRecoverableError(
            'Expected {0} one {1} capability. got {2}'.format(
                'at most' if if_exists else 'exactly', type_name, len(ids)))
    return ids[0] if ids else None


def get_resource_id(ctx, type_name):
    if ctx.node.properties['resource_id']:
        return ctx.node.properties['resource_id']
    return "{0}_{1}_{2}".format(type_name, ctx.deployment.id, ctx.instance.id)


def get_property(ctx, property_name, kwargs={}, default=None):
    return kwargs.get(
        property_name,
        ctx.node.properties.get(property_name, default)
    )


def transform_resource_name(ctx, res):

    if isinstance(res, basestring):
        res = {'name': res}

    if not isinstance(res, dict):
        raise ValueError("transform_resource_name() expects either string or "
                         "dict as the first parameter")

    pfx = ctx.bootstrap_context.resources_prefix

    if not pfx:
        return res['name']

    name = res['name']
    res['name'] = pfx + name

    if name.startswith(pfx):
        ctx.logger.warn("Prefixing resource '{0}' with '{1}' but it "
                        "already has this prefix".format(name, pfx))
    else:
        ctx.logger.info("Transformed resource name '{0}' to '{1}'".format(
                        name, res['name']))

    return res['name']


def _get_resource_by_name_or_id_from_ctx(ctx, name_field_name, openstack_type,
                                         sugared_client):
    resource_id = ctx.instance.runtime_properties.get(
        OPENSTACK_ID_PROPERTY, ctx.node.properties['resource_id'])
    if not resource_id:
        raise NonRecoverableError(
            "Can't set '{0}' to True without supplying a value for "
            "'resource_id'".format(USE_EXTERNAL_RESOURCE_PROPERTY))

    return get_resource_by_name_or_id(resource_id, openstack_type,
                                      sugared_client, True, name_field_name)


def get_resource_by_name_or_id(
        resource_id, openstack_type, sugared_client,
        raise_if_not_found=True, name_field_name='name'):

    # search for resource by name (or name-equivalent field)
    search_param = {name_field_name: resource_id}
    resource = sugared_client.cosmo_get_if_exists(openstack_type,
                                                  **search_param)
    if not resource:
        # fallback - search for resource by id
        resource = sugared_client.cosmo_get_if_exists(
            openstack_type, id=resource_id)

    if not resource and raise_if_not_found:
        raise NonRecoverableError(
            MISSING_RESOURCE_MESSAGE.format(openstack_type, resource_id))

    return resource


def use_external_resource(ctx, sugared_client, openstack_type,
                          name_field_name='name'):
    if not is_external_resource(ctx):
        return None
    try:
        resource = _get_resource_by_name_or_id_from_ctx(
            ctx, name_field_name, openstack_type, sugared_client)
    except NonRecoverableError:
        if is_create_if_missing(ctx):
            ctx.instance.runtime_properties[CONDITIONALLY_CREATED] = True
            return None
        else:
            raise

    ctx.instance.runtime_properties[OPENSTACK_ID_PROPERTY] = \
        sugared_client.get_id_from_resource(resource)
    ctx.instance.runtime_properties[OPENSTACK_TYPE_PROPERTY] = openstack_type
    ctx.instance.runtime_properties[OPENSTACK_RESOURCE_PROPERTY] = \
        resource if isinstance(resource, dict) else resource.to_dict()

    from openstack_plugin_common.floatingip import FLOATINGIP_OPENSTACK_TYPE
    # store openstack name runtime property, unless it's a floating IP type,
    # in which case the ip will be stored in the runtime properties instead.
    if openstack_type != FLOATINGIP_OPENSTACK_TYPE:
        ctx.instance.runtime_properties[OPENSTACK_NAME_PROPERTY] = \
            sugared_client.get_name_from_resource(resource)

    ctx.logger.info('Using external resource {0}: {1}'.format(
        openstack_type, ctx.instance.runtime_properties.get(
            OPENSTACK_NAME_PROPERTY, ctx.node.properties['resource_id'])))
    return resource


def validate_resource(ctx, sugared_client, openstack_type,
                      name_field_name='name'):
    ctx.logger.debug('validating resource {0} (node {1})'.format(
        openstack_type, ctx.node.id))

    openstack_type_plural = sugared_client.cosmo_plural(openstack_type)
    resource = None

    if is_external_resource(ctx):

        try:
            # validate the resource truly exists
            resource = _get_resource_by_name_or_id_from_ctx(
                ctx, name_field_name, openstack_type, sugared_client)
            ctx.logger.debug('OK: {0} {1} found in pool'.format(
                openstack_type, ctx.node.properties['resource_id']))
        except NonRecoverableError as e:
            if not is_create_if_missing(ctx):
                ctx.logger.error('VALIDATION ERROR: ' + str(e))
                resource_list = list(sugared_client.cosmo_list(openstack_type))
                if resource_list:
                    ctx.logger.info('list of existing {0}: '.format(
                        openstack_type_plural))
                    for resource in resource_list:
                        ctx.logger.info('    {0:>10} - {1}'.format(
                            sugared_client.get_id_from_resource(resource),
                            sugared_client.get_name_from_resource(resource)))
                else:
                    ctx.logger.info('there are no existing {0}'.format(
                        openstack_type_plural))
                raise
    if not resource:
        if isinstance(sugared_client, NovaClientWithSugar):
            # not checking quota for Nova resources due to a bug in Nova client
            return

        # validate available quota for provisioning the resource
        resource_list = list(sugared_client.cosmo_list(openstack_type))
        resource_amount = len(resource_list)

        resource_quota = sugared_client.get_quota(openstack_type)

        if resource_amount < resource_quota \
                or resource_quota == INFINITE_RESOURCE_QUOTA:
            ctx.logger.debug(
                'OK: {0} (node {1}) can be created. provisioned {2}: {3}, '
                'quota: {4}'
                .format(openstack_type, ctx.node.id, openstack_type_plural,
                        resource_amount, resource_quota))
        else:
            err = ('{0} (node {1}) cannot be created due to quota limitations.'
                   ' provisioned {2}: {3}, quota: {4}'
                   .format(openstack_type, ctx.node.id, openstack_type_plural,
                           resource_amount, resource_quota))
            ctx.logger.error('VALIDATION ERROR:' + err)
            raise NonRecoverableError(err)


def delete_resource_and_runtime_properties(ctx, sugared_client,
                                           runtime_properties_keys):
    node_openstack_type = ctx.instance.runtime_properties[
        OPENSTACK_TYPE_PROPERTY]

    if not is_external_resource_not_conditionally_created(ctx):
        ctx.logger.info('deleting {0}'.format(node_openstack_type))

        sugared_client.cosmo_delete_resource(
            node_openstack_type,
            ctx.instance.runtime_properties[OPENSTACK_ID_PROPERTY])
    else:
        ctx.logger.info('not deleting {0} since an external {0} is '
                        'being used'.format(node_openstack_type))

    delete_runtime_properties(ctx, runtime_properties_keys)


def is_external_resource(ctx):
    return is_external_resource_by_properties(ctx.node.properties)


def is_external_resource_not_conditionally_created(ctx):
    return is_external_resource_by_properties(ctx.node.properties) and \
        not ctx.instance.runtime_properties.get(CONDITIONALLY_CREATED)


def is_external_relationship_not_conditionally_created(ctx):
    return is_external_resource_by_properties(ctx.source.node.properties) and \
        is_external_resource_by_properties(ctx.target.node.properties) and \
        not ctx.source.instance.runtime_properties.get(
            CONDITIONALLY_CREATED) and not \
        ctx.target.instance.runtime_properties.get(CONDITIONALLY_CREATED)


def is_create_if_missing(ctx):
    return is_create_if_missing_by_properties(ctx.node.properties)


def is_external_relationship(ctx):
    return is_external_resource_by_properties(ctx.source.node.properties) and \
        is_external_resource_by_properties(ctx.target.node.properties)


def is_external_resource_by_properties(properties):
    return USE_EXTERNAL_RESOURCE_PROPERTY in properties and \
        properties[USE_EXTERNAL_RESOURCE_PROPERTY]


def is_create_if_missing_by_properties(properties):
    return CREATE_IF_MISSING_PROPERTY in properties and \
        properties[CREATE_IF_MISSING_PROPERTY]


def delete_runtime_properties(ctx, runtime_properties_keys):
    for runtime_prop_key in runtime_properties_keys:
        if runtime_prop_key in ctx.instance.runtime_properties:
            del ctx.instance.runtime_properties[runtime_prop_key]


def validate_ip_or_range_syntax(ctx, address, is_range=True):
    range_suffix = ' range' if is_range else ''
    ctx.logger.debug('checking whether {0} is a valid address{1}...'
                     .format(address, range_suffix))
    try:
        IP(address)
        ctx.logger.debug('OK:'
                         '{0} is a valid address{1}.'.format(address,
                                                             range_suffix))
    except ValueError as e:
        err = ('{0} is not a valid address{1}; {2}'.format(
            address, range_suffix, e.message))
        ctx.logger.error('VALIDATION ERROR:' + err)
        raise NonRecoverableError(err)


def get_openstack_id(ctx):
    return ctx.instance.runtime_properties[OPENSTACK_ID_PROPERTY]


def get_openstack_type(ctx):
    return ctx.instance.runtime_properties[OPENSTACK_TYPE_PROPERTY]


def create_object_dict(ctx, object_name, args, object_dict=None):
    object_dict = object_dict if object_dict is not None else {}
    object_dict['name'] = get_resource_id(ctx, object_name)
    object_dict.update(ctx.node.properties[object_name], **args)
    transform_resource_name(ctx, object_dict)
    return object_dict


def add_list_to_runtime_properties(ctx, openstack_type_name, object_list):

    objects = []

    for obj in object_list:
        if type(obj) not in [str, dict]:
            obj = obj.to_dict()
        objects.append(obj)

    ctx.instance.runtime_properties[openstack_type_name + '_list'] \
        = objects
    return objects


def set_openstack_runtime_properties(ctx, openstack_object, openstack_type):
    ctx.instance.runtime_properties[OPENSTACK_ID_PROPERTY] = \
        openstack_object.id
    ctx.instance.runtime_properties[OPENSTACK_TYPE_PROPERTY] = \
        openstack_type
    ctx.instance.runtime_properties[OPENSTACK_NAME_PROPERTY] = \
        openstack_object.name


def set_neutron_runtime_properties(ctx, openstack_object, openstack_type):
    ctx.instance.runtime_properties[OPENSTACK_ID_PROPERTY] = \
        openstack_object['id']
    ctx.instance.runtime_properties[OPENSTACK_TYPE_PROPERTY] = \
        openstack_type
    ctx.instance.runtime_properties[OPENSTACK_NAME_PROPERTY] = \
        openstack_object['name']


class Config(object):

    OPENSTACK_CONFIG_PATH_ENV_VAR = 'OPENSTACK_CONFIG_PATH'
    OPENSTACK_CONFIG_PATH_DEFAULT_PATH = '~/openstack_config.json'
    OPENSTACK_ENV_VAR_PREFIX = 'OS_'
    OPENSTACK_SUPPORTED_ENV_VARS = {
        'OS_AUTH_URL', 'OS_USERNAME', 'OS_PASSWORD', 'OS_TENANT_NAME',
        'OS_REGION_NAME', 'OS_PROJECT_ID', 'OS_PROJECT_NAME',
        'OS_USER_DOMAIN_NAME', 'OS_PROJECT_DOMAIN_NAME'
    }

    @classmethod
    def get(cls):
        static_config = cls._build_config_from_env_variables()
        env_name = cls.OPENSTACK_CONFIG_PATH_ENV_VAR
        default_location_tpl = cls.OPENSTACK_CONFIG_PATH_DEFAULT_PATH
        default_location = os.path.expanduser(default_location_tpl)
        config_path = os.getenv(env_name, default_location)
        try:
            with open(config_path) as f:
                cls.update_config(static_config, json.loads(f.read()))
        except IOError:
            pass
        return static_config

    @classmethod
    def _build_config_from_env_variables(cls):
        return {v.lstrip(cls.OPENSTACK_ENV_VAR_PREFIX).lower(): os.environ[v]
                for v in cls.OPENSTACK_SUPPORTED_ENV_VARS if v in os.environ}

    @staticmethod
    def update_config(overridden_cfg, overriding_cfg):
        """ this method is like dict.update() only that it doesn't override
        with (or set new) empty values (e.g. empty string) """
        for k, v in overriding_cfg.iteritems():
            if v:
                overridden_cfg[k] = v


class OpenStackClient(object):

    COMMON = {'username', 'password', 'auth_url'}
    AUTH_SETS = [
        COMMON | {'tenant_name'},
        COMMON | {'project_id', 'user_domain_name'},
        COMMON | {'project_id', 'project_name', 'user_domain_name'},
        COMMON | {'project_name', 'user_domain_name', 'project_domain_name'},
    ]

    NON_AUTH_ITEMS = ['region', 'insecure',
                      'ca_cert', 'nova_url',
                      'neutron_url', 'custom_configuration', 'logging']

    OPTIONAL_AUTH_PARAMS = {AUTH_PARAM_INSECURE, AUTH_PARM_CA_CERT}

    def __init__(self, client_name, client_class, config=None, *args, **kw):
        cfg = Config.get()
        if config:
            Config.update_config(cfg, config)

            # This check to make sure that blueprint openstack config
            # contains all the required auth params + any non-auth param
            if set(config.keys())\
                    in self.AUTH_SETS and config.keys() in self.NON_AUTH_ITEMS:

                # Check if there is any value exists on ``cfg``
                # that does not exist on ``config`` then these extra params
                # should be removed to prevent any merging conflicts
                removed_keys = []
                for k, v in cfg.iteritems():
                    if k not in config:
                        removed_keys.append(k)

                for key in removed_keys:
                    del cfg[key]

        v3 = '/v3' in cfg['auth_url']
        # Newer libraries expect the region key to be `region_name`, not
        # `region`.
        region = cfg.pop('region', None)
        if v3 and region:
            cfg['region_name'] = region

        # Calculate effective logging policy.
        # Note that we don't use dict's update() function at the dict's
        # root because it will overwrite nested dicts.

        logging_config = cfg.pop('logging', dict())
        use_cfy_logger = logging_config.get(KEY_USE_CFY_LOGGER)
        groups_config = logging_config.get(KEY_GROUPS, {})
        loggers_config = logging_config.get(KEY_LOGGERS, {})

        final_logging_cfg = copy.deepcopy(DEFAULT_LOGGING_CONFIG)

        if use_cfy_logger:
            final_logging_cfg[KEY_USE_CFY_LOGGER] = use_cfy_logger
        else:
            use_cfy_logger = final_logging_cfg[KEY_USE_CFY_LOGGER]
        final_logging_cfg[KEY_GROUPS].update(groups_config)
        final_logging_cfg[KEY_LOGGERS].update(loggers_config)

        # Prepare mapping between logger names and logging levels.
        configured_loggers = {v: final_logging_cfg[KEY_GROUPS][k] for
                              k, values in LOGGING_GROUPS.items() for v
                              in values}
        configured_loggers.update(final_logging_cfg[KEY_LOGGERS])

        ctx_log_handler = CloudifyCtxLogHandler(ctx) if use_cfy_logger \
            else None

        for logger_name, logger_level in configured_loggers.items():
            logger = logging.getLogger(logger_name)
            if ctx_log_handler:
                logger.addHandler(ctx_log_handler)
            logger.setLevel(logger_level)

        cfg = self._merge_custom_configuration(cfg, client_name)

        auth_params, client_params = OpenStackClient._split_config(cfg)
        OpenStackClient._validate_auth_params(auth_params)

        if v3:
            # keystone v3 complains if these aren't set.
            for key in 'user_domain_name', 'project_domain_name':
                auth_params.setdefault(key, 'default')

        client_params['session'] = self._authenticate(auth_params)
        self._client = client_class(**client_params)

    @classmethod
    def _validate_auth_params(cls, params):
        if set(params.keys()) - cls.OPTIONAL_AUTH_PARAMS in cls.AUTH_SETS:
            return

        def set2str(s):
            return '({})'.format(', '.join(sorted(s)))

        received_params = set2str(params)
        valid_auth_sets = map(set2str, cls.AUTH_SETS)
        raise NonRecoverableError(
            "{} is not valid set of auth params. Expected to find parameters "
            "either as environment variables, in a JSON file (at either a "
            "path which is set under the environment variable {} or at the "
            "default location {}), or as nested properties under an "
            "'{}' property. Valid auth param sets are: {}."
            .format(received_params,
                    Config.OPENSTACK_CONFIG_PATH_ENV_VAR,
                    Config.OPENSTACK_CONFIG_PATH_DEFAULT_PATH,
                    CONFIG_PROPERTY,
                    ', '.join(valid_auth_sets)))

    @staticmethod
    def _merge_custom_configuration(cfg, client_name):
        config = cfg.copy()

        mapping = {
            'nova_url': 'nova_client',
            'neutron_url': 'neutron_client'
        }
        for key in 'nova_url', 'neutron_url':
            val = config.pop(key, None)
            if val is not None:
                ctx.logger.warn(
                    "'{}' property is deprecated. Use `custom_configuration"
                    ".{}.endpoint_override` instead.".format(
                        key, mapping[key]))
                if mapping.get(key, None) == client_name:
                    config['endpoint_override'] = val

        if 'custom_configuration' in cfg:
            del config['custom_configuration']
            config.update(cfg['custom_configuration'].get(client_name, {}))
        return config

    @classmethod
    def _split_config(cls, cfg):
        all = reduce(lambda x, y: x | y, cls.AUTH_SETS)
        all |= cls.OPTIONAL_AUTH_PARAMS

        auth, misc = {}, {}
        for param, value in cfg.items():
            if param in all:
                auth[param] = value
            else:
                misc[param] = value
        return auth, misc

    @staticmethod
    def _authenticate(cfg):
        # 'verify' will contain one of the following:
        #
        # True: perform certificate validation against the underlying
        #       CA certs bundle (note: this is the certs bundle used
        #       by the 'requests' library, which is different from the
        #       OS-provided one).
        #
        #       To get that, specify 'insecure: True'.
        #
        # False: disable certificate validation altogether.
        #
        #       To get that, specify 'insecure: False' (or any value
        #       other than True).
        #
        # Any other string: path to the CA cert (or bundle) to verify
        #                   against.
        #
        #                   To get that, specify 'ca_cert: path_to_file'
        #                   and ensure 'insecure' is NOT specified.
        verify = True
        if AUTH_PARAM_INSECURE in cfg:
            cfg = cfg.copy()
            # NOTE: Next line will evaluate to False only when insecure is set
            # to True. Any other value (string etc.) will force verify to True.
            # This is done on purpose, since we do not wish to use insecure
            # connection by mistake.
            # Update: We are adding handling for casting 'True' or 'true' as a
            # bool because if this value is set via an instrinsic function
            # it will always be a string.
            cfg_insecure = cfg[AUTH_PARAM_INSECURE]
            if isinstance(cfg_insecure, basestring) and \
                    cfg_insecure.capitalize() == 'True':
                cfg[AUTH_PARAM_INSECURE] = True
            verify = not (cfg[AUTH_PARAM_INSECURE] is True)
            del cfg[AUTH_PARAM_INSECURE]
        elif AUTH_PARM_CA_CERT in cfg:
            cfg = cfg.copy()
            verify = cfg[AUTH_PARM_CA_CERT]
            del cfg[AUTH_PARM_CA_CERT]

        loader = loading.get_plugin_loader("password")
        auth = loader.load_from_options(**cfg)
        sess = session.Session(auth=auth, verify=verify)
        return sess

    # Proxy any unknown call to base client
    def __getattr__(self, attr):
        return getattr(self._client, attr)

    # Sugar, common to all clients
    def cosmo_plural(self, obj_type_single):
        return obj_type_single + 's'

    def cosmo_get_named(self, obj_type_single, name, **kw):
        return self.cosmo_get(obj_type_single, name=name, **kw)

    def cosmo_get(self, obj_type_single, **kw):
        return self._cosmo_get(obj_type_single, False, **kw)

    def cosmo_get_if_exists(self, obj_type_single, **kw):
        return self._cosmo_get(obj_type_single, True, **kw)

    def _cosmo_get(self, obj_type_single, if_exists, **kw):
        ls = list(self.cosmo_list(obj_type_single, **kw))
        check = len(ls) > 1 if if_exists else len(ls) != 1
        if check:
            raise NonRecoverableError(
                "Expected {0} one object of type {1} "
                "with match {2} but there are {3}".format(
                    'at most' if if_exists else 'exactly',
                    obj_type_single, kw, len(ls)))
        return ls[0] if ls else None


class GlanceClient(OpenStackClient):

    # Can't glance_url be figured out from keystone
    REQUIRED_CONFIG_PARAMS = \
        ['username', 'password', 'tenant_name', 'auth_url']

    def connect(self, cfg):
        loader = loading.get_plugin_loader('password')
        auth = loader.load_from_options(
            auth_url=cfg['auth_url'],
            username=cfg['username'],
            password=cfg['password'],
            tenant_name=cfg['tenant_name'])
        sess = session.Session(auth=auth)

        client_kwargs = dict(
            session=sess,
        )
        if cfg.get('glance_url'):
            client_kwargs['endpoint'] = cfg['glance_url']

        return GlanceClientWithSugar(**client_kwargs)


# Decorators
def _find_instanceof_in_kw(cls, kw):
    ret = [v for v in kw.values() if isinstance(v, cls)]
    if not ret:
        return None
    if len(ret) > 1:
        raise NonRecoverableError(
            "Expected to find exactly one instance of {0} in "
            "kwargs but found {1}".format(cls, len(ret)))
    return ret[0]


def _find_context_in_kw(kw):
    return _find_instanceof_in_kw(cloudify.context.CloudifyContext, kw)


def with_neutron_client(f):
    @wraps(f)
    def wrapper(*args, **kw):
        _handle_kw('neutron_client', NeutronClientWithSugar, kw)

        try:
            return f(*args, **kw)
        except neutron_exceptions.NeutronClientException as e:
            if e.status_code in _non_recoverable_error_codes:
                _re_raise(e, recoverable=False, status_code=e.status_code)
            else:
                raise
    return wrapper


def with_nova_client(f):
    @wraps(f)
    def wrapper(*args, **kw):
        _handle_kw('nova_client', NovaClientWithSugar, kw)

        try:
            return f(*args, **kw)
        except nova_exceptions.OverLimit as e:
            _re_raise(e, recoverable=True, retry_after=e.retry_after)
        except nova_exceptions.ClientException as e:
            if e.code in _non_recoverable_error_codes:
                _re_raise(e, recoverable=False, status_code=e.code)
            else:
                raise
    return wrapper


def with_cinder_client(f):
    @wraps(f)
    def wrapper(*args, **kw):
        _handle_kw('cinder_client', CinderClientWithSugar, kw)

        try:
            return f(*args, **kw)
        except cinder_exceptions.ClientException as e:
            if e.code in _non_recoverable_error_codes:
                _re_raise(e, recoverable=False, status_code=e.code)
            else:
                raise
    return wrapper


def with_glance_client(f):
    @wraps(f)
    def wrapper(*args, **kw):
        _handle_kw('glance_client', GlanceClientWithSugar, kw)

        try:
            return f(*args, **kw)
        except glance_exceptions.ClientException as e:
            if e.code in _non_recoverable_error_codes:
                _re_raise(e, recoverable=False, status_code=e.code)
            else:
                raise
    return wrapper


def with_keystone_client(f):
    @wraps(f)
    def wrapper(*args, **kw):
        _handle_kw('keystone_client', KeystoneClientWithSugar, kw)

        try:
            return f(*args, **kw)
        except keystone_exceptions.HTTPError as e:
            if e.http_status in _non_recoverable_error_codes:
                _re_raise(e, recoverable=False, status_code=e.http_status)
            else:
                raise
        except keystone_exceptions.ClientException as e:
            _re_raise(e, recoverable=False)
    return wrapper


def _handle_kw(client_name, client_class, kw):

    _ctx = _find_context_in_kw(kw) or ctx
    if _ctx.type == context.NODE_INSTANCE:
        config = _ctx.node.properties.get(CONFIG_PROPERTY)
        resource_id = kw.pop(
            'resource_id', _ctx.node.properties.get('resource_id'))
        rt_config = _ctx.instance.runtime_properties.get(
            CONFIG_RUNTIME_PROPERTY)
        if resource_id and OPENSTACK_ID_PROPERTY not in \
                _ctx.instance.runtime_properties:
            _ctx.instance.runtime_properties[OPENSTACK_ID_PROPERTY] = \
                resource_id
    elif _ctx.type == context.RELATIONSHIP_INSTANCE:
        config = _ctx.source.node.properties.get(CONFIG_PROPERTY)
        rt_config = _ctx.source.instance.runtime_properties.get(
            CONFIG_RUNTIME_PROPERTY)
        resource_id = kw.pop(
            'resource_id', _ctx.source.node.properties.get('resource_id'))
        if not config:
            config = _ctx.target.node.properties.get(CONFIG_PROPERTY)
            rt_config = _ctx.target.instance.runtime_properties.get(
                CONFIG_RUNTIME_PROPERTY)
        if resource_id and OPENSTACK_ID_PROPERTY not in \
                _ctx.source.instance.runtime_properties:
            _ctx.source.instance.runtime_properties[OPENSTACK_ID_PROPERTY] = \
                resource_id
    else:
        config = None
        rt_config = None

    # Overlay with configuration from runtime property, if any.
    if rt_config:
        if config:
            config = config.copy()
            config.update(rt_config)
        else:
            config = rt_config

    if client_name in kw:
        return
    if CONFIG_INPUT in kw:
        if config:
            config = config.copy()
            config.update(kw[CONFIG_INPUT])
        else:
            config = kw[CONFIG_INPUT]
    kw[client_name] = client_class(config=config)


_non_recoverable_error_codes = [400, 401, 403, 404, 409]


def _re_raise(e, recoverable, retry_after=None, status_code=None):
    exc_type, exc, traceback = sys.exc_info()
    message = e.message
    if status_code is not None:
        message = '{0} [status_code={1}]'.format(message, status_code)
    if recoverable:
        if retry_after == 0:
            retry_after = None
        raise RecoverableError(
            message=message,
            retry_after=retry_after), None, traceback
    else:
        raise NonRecoverableError(message), None, traceback


# Sugar for clients

class NovaClientWithSugar(OpenStackClient):

    def __init__(self, *args, **kw):
        config = kw['config']
        if config.get('nova_url'):
            config['endpoint_override'] = config.pop('nova_url')

        try:
            nova_client_version = \
                config['custom_configuration']['nova_client'].pop(
                    'version')
        except KeyError:
            nova_client_version = '2'

        # In case someone provides an int.
        if not isinstance(nova_client_version, basestring):
            nova_client_version = str(nova_client_version)

        super(NovaClientWithSugar, self).__init__(
            'nova_client',
            partial(nova_client.Client, nova_client_version), *args, **kw)

    def cosmo_list(self, obj_type_single, **kw):
        """ Sugar for xxx.findall() - not using xxx.list() because findall
        can receive filtering parameters, and it's common for all types"""
        obj_type_plural = self._get_nova_field_name_for_type(obj_type_single)
        for obj in getattr(self, obj_type_plural).findall(**kw):
            yield obj

    def cosmo_delete_resource(self, obj_type_single, obj_id):
        obj_type_plural = self._get_nova_field_name_for_type(obj_type_single)
        getattr(self, obj_type_plural).delete(obj_id)

    def get_id_from_resource(self, resource):
        return resource.id

    def get_name_from_resource(self, resource):
        return resource.name

    def get_quota(self, obj_type_single):
        raise RuntimeError(
            'Retrieving quotas from Nova service is currently unsupported '
            'due to a bug in Nova python client')

        # we're already authenticated, but the following call will make
        # 'service_catalog' available under 'client', through which we can
        # extract the tenant_id (Note that self.client.tenant_id might be
        # None if project_id (AKA tenant_name) was used instead; However the
        # actual tenant_id must be used to retrieve the quotas)
        self.client.authenticate()
        tenant_id = self.client.service_catalog.get_tenant_id()
        quotas = self.quotas.get(tenant_id)
        return getattr(quotas, self.cosmo_plural(obj_type_single))

    def _get_nova_field_name_for_type(self, obj_type_single):
        from openstack_plugin_common.floatingip import \
            FLOATINGIP_OPENSTACK_TYPE
        if obj_type_single == FLOATINGIP_OPENSTACK_TYPE:
            # since we use the same 'openstack type' property value for both
            # neutron and nova floating-ips, this adjustment must be made
            # for nova client, as fields names differ between the two clients
            obj_type_single = 'floating_ip'
        return self.cosmo_plural(obj_type_single)


class NeutronClientWithSugar(OpenStackClient):

    def __init__(self, *args, **kw):
        super(NeutronClientWithSugar, self).__init__(
            'neutron_client', neutron_client.Client, *args, **kw)

    def cosmo_list(self, obj_type_single, **kw):
        """ Sugar for list_XXXs()['XXXs'] """
        obj_type_plural = self.cosmo_plural(obj_type_single)
        for obj in getattr(self, 'list_' + obj_type_plural)(**kw)[
                obj_type_plural]:
            yield obj

    def cosmo_delete_resource(self, obj_type_single, obj_id):
        getattr(self, 'delete_' + obj_type_single)(obj_id)

    def get_id_from_resource(self, resource):
        return resource['id']

    def get_name_from_resource(self, resource):
        return resource['name']

    def get_quota(self, obj_type_single):
        tenant_id = self.get_quotas_tenant()['tenant']['tenant_id']
        quotas = self.show_quota(tenant_id)['quota']
        return quotas[obj_type_single]

    def cosmo_list_prefixed(self, obj_type_single, name_prefix):
        for obj in self.cosmo_list(obj_type_single):
            if obj['name'].startswith(name_prefix):
                yield obj

    def cosmo_delete_prefixed(self, name_prefix):
        # Cleanup all neutron.list_XXX() objects with names starting
        #  with self.name_prefix
        for obj_type_single in 'port', 'router', 'network', 'subnet',\
                               'security_group':
            for obj in self.cosmo_list_prefixed(obj_type_single, name_prefix):
                if obj_type_single == 'router':
                    ports = self.cosmo_list('port', device_id=obj['id'])
                    for port in ports:
                        try:
                            self.remove_interface_router(
                                port['device_id'],
                                {'port_id': port['id']})
                        except neutron_exceptions.NeutronClientException:
                            pass
                getattr(self, 'delete_' + obj_type_single)(obj['id'])

    def cosmo_find_external_net(self):
        """ For tests of floating IP """
        nets = self.list_networks()['networks']
        ls = [net for net in nets if net.get('router:external')]
        if len(ls) != 1:
            raise NonRecoverableError(
                "Expected exactly one external network but found {0}".format(
                    len(ls)))
        return ls[0]


class CinderClientWithSugar(OpenStackClient):

    def __init__(self, *args, **kw):
        super(CinderClientWithSugar, self).__init__(
            'cinder_client', partial(cinder_client.Client, '2'), *args, **kw)

    def cosmo_list(self, obj_type_single, **kw):
        obj_type_plural = self.cosmo_plural(obj_type_single)
        for obj in getattr(self, obj_type_plural).findall(**kw):
            yield obj

    def cosmo_delete_resource(self, obj_type_single, obj_id):
        obj_type_plural = self.cosmo_plural(obj_type_single)
        getattr(self, obj_type_plural).delete(obj_id)

    def get_id_from_resource(self, resource):
        return resource.id

    def get_name_from_resource(self, resource):
        return resource.name

    def get_quota(self, obj_type_single):
        # we're already authenticated, but the following call will make
        # 'service_catalog' available under 'client', through which we can
        # extract the tenant_id (Note that self.client.tenant_id might be
        # None if project_id (AKA tenant_name) was used instead; However the
        # actual tenant_id must be used to retrieve the quotas)
        self.client.authenticate()
        project_id = self.client.session.get_project_id()
        quotas = self.quotas.get(project_id)
        return getattr(quotas, self.cosmo_plural(obj_type_single))


class KeystoneClientWithSugar(OpenStackClient):
    # keystone does not have resource quota
    KEYSTONE_INFINITE_RESOURCE_QUOTA = 10**9

    def __init__(self, *args, **kw):
        super(KeystoneClientWithSugar, self).__init__(
            'keystone_client', keystone_client.Client, *args, **kw)

    def cosmo_list(self, obj_type_single, **kw):
        obj_type_plural = self.cosmo_plural(obj_type_single)
        for obj in getattr(self, obj_type_plural).list(**kw):
            yield obj

    def cosmo_delete_resource(self, obj_type_single, obj_id):
        obj_type_plural = self.cosmo_plural(obj_type_single)
        getattr(self, obj_type_plural).delete(obj_id)

    def get_id_from_resource(self, resource):
        return resource.id

    def get_name_from_resource(self, resource):
        return resource.name

    def get_quota(self, obj_type_single):
        return self.KEYSTONE_INFINITE_RESOURCE_QUOTA


class GlanceClientWithSugar(OpenStackClient):
    GLANCE_INIFINITE_RESOURCE_QUOTA = 10**9

    def __init__(self, *args, **kw):
        super(GlanceClientWithSugar, self).__init__(
            'glance_client', partial(glance_client.Client, '2'), *args, **kw)

    def cosmo_list(self, obj_type_single, **kw):
        obj_type_plural = self.cosmo_plural(obj_type_single)
        return getattr(self, obj_type_plural).list(filters=kw)

    def cosmo_delete_resource(self, obj_type_single, obj_id):
        obj_type_plural = self.cosmo_plural(obj_type_single)
        getattr(self, obj_type_plural).delete(obj_id)

    def get_id_from_resource(self, resource):
        return resource.id

    def get_name_from_resource(self, resource):
        return resource.name

    def get_quota(self, obj_type_single):
        return self.GLANCE_INIFINITE_RESOURCE_QUOTA
