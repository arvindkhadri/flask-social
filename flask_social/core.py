
from flask import current_app, redirect
from flask.ext.security import current_user
from flask.ext.oauth import OAuth

from flask_social import exceptions
from flask_social.utils import get_display_name, do_flash, config_value, \
     get_default_provider_names, get_class_from_string


default_config = {
    'SOCIAL_URL_PREFIX': None,
    'SOCIAL_APP_URL': 'http://127.0.0.1:5000',
    'SOCIAL_CONNECT_ALLOW_REDIRECT': '/profile',
    'SOCIAL_CONNECT_DENY_REDIRECT': '/profile',
    'SOCIAL_FLASH_MESSAGES': True,
    'SOCIAL_POST_OAUTH_CONNECT_SESSION_KEY': 'post_oauth_connect_url',
    'SOCIAL_POST_OAUTH_LOGIN_SESSION_KEY': 'post_oauth_login_url'
}


class Provider(object):
    def __init__(self, remote_app, connection_factory,
                 login_handler, connect_handler):
        self.remote_app = remote_app
        self.connection_factory = connection_factory
        self.login_handler = login_handler
        self.connect_handler = connect_handler

    def get_connection(self, *args, **kwargs):
        return self.connection_factory(*args, **kwargs)

    def login_handler(self, *args, **kwargs):
        return self.login_handler(*args, **kwargs)

    def connect_handler(self, *args, **kwargs):
        return self.connect_handler(*args, **kwargs)

    def tokengetter(self, *args, **kwargs):
        return self.remote_app.tokengetter(*args, **kwargs)

    def authorized_handler(self, *args, **kwargs):
        return self.remote_app.authorized_handler(*args, **kwargs)

    def authorize(self, *args, **kwargs):
        return self.remote_app.authorize(*args, **kwargs)

    def __str__(self):
        return '<Provider name=%s>' % self.remote_app.name


class ConnectionFactory(object):
    """The ConnectionFactory class creates `Connection` instances for the
    specified provider from values stored in the connection repository. This
    class should be extended whenever adding a new service provider to an
    application.
    """
    def __init__(self, provider_id):
        """Creates an instance of a `ConnectionFactory` for the specified
        provider

        :param provider_id: The provider ID
        """
        self.provider_id = provider_id

    def _get_current_user_primary_connection(self):
        return self._get_primary_connection(current_user.get_id())

    def _get_primary_connection(self, user_id):
        return current_app.social.datastore.get_primary_connection(
            user_id, self.provider_id)

    def _get_specific_connection(self, user_id, provider_user_id):
        return current_app.social.datastore.get_connection(user_id,
            self.provider_id, provider_user_id)

    def _create_api(self, connection):
        raise NotImplementedError("create_api method not implemented")

    def get_connection(self, user_id=None, provider_user_id=None):
        """Get a connection to the provider for the specified local user
        and the specified provider user

        :param user_id: The local user ID
        :param provider_user_id: The provider user ID
        """
        if user_id == None and provider_user_id == None:
            connection = self._get_current_user_primary_connection()
        if user_id != None and provider_user_id == None:
            connection = self._get_primary_connection(user_id)
        if user_id != None and provider_user_id != None:
            connection = self._get_specific_connection(user_id,
                                                       provider_user_id)

        def as_dict(model):
            rv = {}
            for key in ('user_id', 'provider_id', 'provider_user_id',
                        'access_token', 'secret', 'display_name',
                        'profile_url', 'image_url'):
                rv[key] = getattr(model, key)
            return rv

        return dict(api=self._create_api(connection),
                    **as_dict(connection))

    def __call__(self, **kwargs):
        try:
            return self.get_connection(**kwargs)
        except exceptions.ConnectionNotFoundError:
            return None


class OAuthHandler(object):
    """The `OAuthHandler` class is a base class for classes that handle OAuth
    interactions. See `LoginHandler` and `ConnectHandler`
    """
    def __init__(self, provider_id, callback=None):
        self.provider_id = provider_id
        self.callback = callback


class LoginHandler(OAuthHandler):
    """ A `LoginHandler` handles the login procedure after receiving
    authorization from the service provider. The goal of a `LoginHandler` is
    to retrieve the user ID of the account that granted access to the local
    application. This ID is then used to find a connection within the local
    application to the provider. If a connection is found, the local user is
    retrieved from the user service and logged in autmoatically.
    """
    def get_provider_user_id(self, response):
        """Gets the provider user ID from the OAuth reponse.
        :param response: The OAuth response in the form of a dictionary
        """
        raise NotImplementedError("get_provider_user_id")

    def __call__(self, response):
        display_name = get_display_name(self.provider_id)

        current_app.logger.debug('Received login response from '
                                 '%s: %s' % (display_name, response))

        if response is None:
            do_flash("Access was denied to your %s "
                     "account" % display_name, 'error')

            return redirect(current_app.security.login_manager.login_view)

        uid = self.get_provider_user_id(response)

        return self.callback(self.provider_id, uid, response)


class ConnectHandler(OAuthHandler):
    """The `ConnectionHandler` class handles the connection procedure after
    receiving authorization from the service provider. The goal of a
    `ConnectHandler` is to retrieve the connection values that will be
    persisted by the connection service.
    """
    def get_connection_values(self, response):
        """Get the connection values to persist using values from the OAuth
        response

        :param response: The OAuth response as a dictionary of values
        """
        raise NotImplementedError("get_connection_values")

    def __call__(self, response, user_id=None):
        display_name = get_display_name(self.provider_id)

        current_app.logger.debug('Received connect response from '
                                 '%s. %s' % (display_name, response))

        if response is None:
            do_flash("Access was denied by %s" % display_name, 'error')
            return redirect(config_value('CONNECT_DENY_REDIRECT'))

        cv = self.get_connection_values(response)

        return self.callback(cv, user_id)


class Social(object):

    def __init__(self, app=None, datastore=None):
        self.providers = {}
        self.init_app(app, datastore)

    def init_app(self, app, datastore):
        """Initialize the application with the Social module

        :param app: The Flask application
        :param datastore: Connection datastore instance
        """

        self.datastore = datastore

        for key, value in default_config.items():
            app.config.setdefault(key, value)

        default_provider_names = get_default_provider_names()

        provider_configs = []

        # Look for providers in config
        for key in app.config.keys():
            if key.startswith('SOCIAL_') and key not in default_config:
                provider_id = key.replace('SOCIAL_', '').lower()

                if provider_id not in default_provider_names:
                    # Custom provider, grab the whole config
                    provider_configs.append(app.config.get(key))
                    continue

                # Default provider, update with defaults
                co = 'flask_social.providers.%s::default_config' % provider_id

                d_config = get_class_from_string(co).copy()
                d_oauth_config = d_config['oauth'].copy()

                d_config.update(app.config[key])
                d_oauth_config.update(app.config[key]['oauth'])
                d_config['oauth'] = d_oauth_config

                app.config[key] = d_config

                provider_configs.append(d_config)

        self.oauth = OAuth()

        from flask_social import views

        # Configure the URL handlers for each fo the configured providers
        blueprint = views.create_blueprint(
            app, 'flask_social', __name__,
            url_prefix=config_value('URL_PREFIX', app=app))

        for pc in provider_configs:
            pid, p = views.configure_provider(app, blueprint, self.oauth, pc)
            self.register_provider(pid, p)
            app.logger.debug('Registered social provider: %s' % p)

        app.register_blueprint(blueprint)

        app.social = self

    def register_provider(self, name, provider):
        self.providers[name] = provider

    def __getattr__(self, name):
        return self.providers.get(name, None)