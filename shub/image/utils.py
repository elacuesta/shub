import os
import re
import sys

import click
import yaml
from tqdm import tqdm
from six import binary_type

from shub import config as shub_config
from shub import utils as shub_utils
from shub.exceptions import (
    ShubException, NotFoundException, BadConfigException, RemoteErrorException,
    ShubDeprecationWarning, print_warning
)


DEFAULT_DOCKER_API_VERSION = '1.17'
STATUS_FILE_LOCATION = '.releases'
_VALIDSPIDERNAME = re.compile('^[a-z0-9][-._a-z0-9]+$', re.I)

DOCKER_UNAVAILABLE_MSG = """
Detected error connecting to Docker daemon's host.

Please ensure that you have Docker installed, configured and running locally,
that's essential for running shub image command. To check that run command

    docker version

and check its output: it should contain Docker client and server versions and
should not contain any errors.

You can learn about Docker at https://www.docker.com/.
"""


def is_verbose():
    ctx = click.get_current_context(True)
    return ctx and (ctx.params.get('verbose') or ctx.params.get('debug'))


def debug_log(msg):
    if is_verbose():
        click.echo(msg)


def deprecate_debug_parameter(ctx, param, value):
    if value:
        print_warning("-d/--debug parameter is deprecated. "
                      "Please use -v/--verbose parameter instead.",
                      ShubDeprecationWarning)
    return value


def deprecate_async_parameter(ctx, param, value):
    if value:
        print_warning("--async parameter is deprecated.", ShubDeprecationWarning)
    return value


def get_project_dir():
    """ A helper to get project root dir.
        Used by init/build command to locate Dockerfile.
    """
    closest = shub_utils.closest_file('scrapinghub.yml')
    if not closest:
        raise BadConfigException(
            "Not inside a project: scrapinghub.yml not found.")
    return os.path.dirname(closest)


def get_docker_client(validate=True):
    """A helper to initiate Docker client"""
    try:
        import docker
    except ImportError:
        raise ImportError('You need docker python package installed for the cmd')

    # docker-py (legacy)
    if hasattr(docker, 'Client'):
        docker_client_cls = docker.Client
    # docker >= 2.0
    else:
        docker_client_cls = docker.APIClient

    docker_host = os.environ.get('DOCKER_HOST')
    tls_config = None
    if os.environ.get('DOCKER_TLS_VERIFY', False):
        tls_cert_path = os.environ.get('DOCKER_CERT_PATH')
        if not tls_cert_path:
            tls_cert_path = os.path.join(os.path.expanduser('~'), '.docker')
        apply_path_fun = lambda name: os.path.join(tls_cert_path, name)  # noqa
        tls_config = docker.tls.TLSConfig(
            client_cert=(apply_path_fun('cert.pem'),
                         apply_path_fun('key.pem')),
            verify=apply_path_fun('ca.pem'),
            assert_hostname=False)
        docker_host = docker_host.replace('tcp://', 'https://')
    version = os.environ.get('DOCKER_API_VERSION', DEFAULT_DOCKER_API_VERSION)
    client = docker_client_cls(base_url=docker_host,
                               version=version,
                               tls=tls_config)
    if validate:
        validate_connection_with_docker_daemon(client)
    return client


def validate_connection_with_docker_daemon(client):
    try:
        client.version()
    except:
        raise ShubException(DOCKER_UNAVAILABLE_MSG)


def format_image_name(image_name, image_tag):
    """Format image name using image tag"""
    parts = image_name.rsplit('/', 1)
    # check if tag is already here
    if ':' in parts[-1]:
        # change name to shorter version w/o existing tag
        click.echo('Please use --version param to specify tag')
        image_name = image_name.rsplit(':', 1)[0]
    if not image_tag:
        config = shub_config.load_shub_config()
        image_tag = config.get_version()
    return '{}:{}'.format(image_name, image_tag)


def get_image_registry(image_name):
    """Extract registry host from Docker image name.

    Returns None if registry hostname is not found in the name, meaning
    that default Docker registry should be used.

    Docker image name is defined according to the following rules:
     - name          := [hostname '/'] component ['/' component]*
     - hostname      := hostcomponent ['.' hostcomponent]* [':' port-number]
     - hostcomponent := /([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])/
    https://github.com/docker/distribution/blob/master/reference/reference.go
    """
    components = image_name.split('/')
    if len(components) > 1 and any(sym in components[0] for sym in '.:'):
        return components[0]


def get_credentials(username=None, password=None, insecure=False,
                    apikey=None, target_apikey=None):
    """ A helper function to get credentials based on cmdline options.

    Returns a tuple with 2 strings: (username, password).

    When working with registries where only username matters:
    missing password leads to auth request to registry authentication service
    without 'account' query parameter which breaks login.
    """
    if insecure:
        return None, None
    elif apikey:
        return apikey, ' '
    elif username:
        if password is None:
            raise click.BadParameter(
                'Password is required when passing username.')
        return username, password
    elif password:
        raise click.BadParameter(
            'Username is required when passing password.')
    return target_apikey, ' '


def store_status_url(status_url, limit):
    """Load status file and update it with a url"""
    data = _load_status_file(STATUS_FILE_LOCATION)
    if not data:
        data[0] = status_url
        _update_status_file(data, STATUS_FILE_LOCATION)
        return 0
    for stored_id, stored_url in data.items():
        if stored_url == status_url:
            click.echo("Found same status_url: {}".format(stored_id))
            return stored_id
    status_id = max(data.keys()) + 1
    data[status_id] = status_url
    if len(data) > limit:
        del data[min(data.keys())]
    _update_status_file(data, STATUS_FILE_LOCATION)
    return status_id


def load_status_url(status_id):
    """ Load status url from file by status_id"""
    if not os.path.isfile(STATUS_FILE_LOCATION):
        raise NotFoundException(
            'Status file is not found at {}'.format(STATUS_FILE_LOCATION))
    data = _load_status_file(STATUS_FILE_LOCATION)
    # return latest status url if status id is not provided
    if not isinstance(status_id, int) and data:
        max_status_id = max(data.keys())
        click.echo('Getting results for latest status id {}.'
                   .format(max_status_id))
        return data[max_status_id]
    if status_id not in data:
        raise NotFoundException(
            "Status url with id {} is not found".format(status_id))
    return data[status_id]


def _load_status_file(path):
    """ Open status file and parse it """
    data = {}
    if not os.path.isfile(path):
        return data
    with open(path, 'r') as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise BadConfigException(
                "Error reading releases file:\n{}".format(exc))
    if not isinstance(data, dict):
        raise BadConfigException(
            "Releases file has wrong format ({}).".format(data))
    return data


def _update_status_file(data, path):
    """ Save status file with updated data """
    with open(path, 'w') as status_file:
        yaml.dump(data, status_file, default_flow_style=False)


def valid_spiders(entries):
    """Filter out garbage and only let valid spider names in
    >>> _valid_spiders(['Update rootfs','sony.com', '', 'soa-uk', '182-blink.com'])
    ['182-blink.com', 'soa-uk', 'sony.com']
    >>> _valid_spiders(['-spiders', 'A77aque'])
    ['A77aque']
    """
    return sorted(filter(_VALIDSPIDERNAME.match, entries))


def ensure_unicode(s, encoding='utf-8'):
    return s.decode(encoding) if isinstance(s, binary_type) else s


class BaseProgress(object):
    """Small helper class to track progress.

    Base implementation stores events iterator and walks through it with
    show() method, handle_event() logic mostly depends on operation.
    """
    def __init__(self, events):
        self.events = events

    def show(self):
        for event in self.events:
            self.handle_event(event)

    def handle_event(self, event):
        if 'error' in event:
            tqdm.write("Error {}: {}".format(event['error'],
                                             event['errorDetail']))
            raise RemoteErrorException("Docker operation failed")


class ProgressBar(tqdm):
    """Fixed version of tqdm.tqdm progress bar."""

    def moveto(self, *args, **kwargs):
        super(ProgressBar, self).moveto(*args, **kwargs)
        if hasattr(self.fp, 'flush'):
            self.fp.flush()


def create_progress_bar(total, desc, **kwargs):
    """Helper creating a progress bar instance for a given parameters set.

    The bar should be closed by calling close() method.
    """
    return ProgressBar(
        total=total,
        desc=desc,
        # XXX: click.get_text_stream or click.get_binary_stream don't
        # work well with tqdm on Windows and Python 3
        file=sys.stdout,
        # helps to update bars on resizing terminal
        dynamic_ncols=True,
        # miniters improves progress on erratic updates caused by network
        miniters=1,
        **kwargs
    )
