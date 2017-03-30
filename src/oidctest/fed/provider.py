from future.backports.urllib.parse import parse_qs
from future.backports.urllib.parse import splitquery
from future.backports.urllib.parse import urlencode

import copy
import json
import logging
import time

import requests
from Cryptodome.PublicKey import RSA
from fedoidc import provider
from jwkest.ecc import P256
from jwkest.jwk import ECKey
from jwkest.jwk import RSAKey
from jwkest.jwk import SYMKey
from oic import oic
from oic.oauth2 import error
from oic.oauth2 import error_response
from oic.oic.message import AccessTokenRequest
from oic.oic.message import ProviderConfigurationResponse
from oic.oic.message import RegistrationRequest
from oic.oic.message import RegistrationResponse
from oic.oic.provider import InvalidRedirectURIError
from oic.utils.keyio import key_summary
from oic.utils.keyio import keyjar_init
from otest.events import EV_EXCEPTION
from otest.events import EV_FAULT
from otest.events import EV_HTTP_RESPONSE
from otest.events import EV_PROTOCOL_REQUEST
from otest.events import EV_REQUEST

__author__ = 'roland'

logger = logging.getLogger(__name__)


class TestError(Exception):
    pass


def unwrap_exception(err):
    while True:  # Exception wrapped in ..
        if not isinstance(err.args, tuple):
            err = err.args
            break
        elif isinstance(err.args[0], Exception):
            err = err.args[0]
        elif len(err.args) > 1 and isinstance(err.args[1], Exception):
            err = err.args[1]
        elif len(err.args) == 1:
            err = err.args[0]
            break
        else:
            err = '{}:{}'.format(*err.args)  # Is this a fair assumption ??
            break
    return err


def sort_string(string):
    if string is None:
        return ""

    _l = list(string)
    _l.sort()
    return "".join(_l)


def response_type_cmp(allowed, offered):
    """

    :param allowed: A list of space separated lists of return types
    :param offered: A space separated list of return types
    :return:
    """

    if ' ' in offered:
        ort = set(offered.split(' '))
    else:
        try:
            ort = {offered}
        except TypeError:  # assume list
            ort = [set(o.split(' ')) for o in offered][0]

    for rt in allowed:
        if ' ' in rt:
            _rt = set(rt.split(' '))
        else:
            _rt = {rt}

        if _rt == ort:
            return True

    return False


class Server(oic.Server):
    def __init__(self, keyjar=None, ca_certs=None, verify_ssl=True):
        oic.Server.__init__(self, keyjar, ca_certs, verify_ssl)

        self.behavior_type = {}


class Provider(provider.Provider):
    def __init__(self, name, sdb, cdb, authn_broker, userinfo, authz,
                 client_authn, symkey, urlmap=None, ca_certs="", keyjar=None,
                 hostname="", template_lookup=None, template=None,
                 verify_ssl=True, capabilities=None, client_cert=None,
                 federation_entity=None, fo_priority=None,
                 **kwargs):

        provider.Provider.__init__(
            self, name, sdb, cdb, authn_broker, userinfo, authz,
            client_authn,
            symkey, urlmap, ca_certs, keyjar, hostname, template_lookup,
            template, verify_ssl, capabilities, client_cert=client_cert,
            federation_entity=federation_entity, fo_priority=fo_priority)

        self.claims_type = ["normal"]
        self.behavior_type = []
        self.server = Server(keyjar=keyjar, ca_certs=ca_certs,
                             verify_ssl=verify_ssl)
        self.server.behavior_type = self.behavior_type
        self.claim_access_token = {}
        self.init_keys = []
        self.update_key_use = ""
        for param in ['jwks_name', 'jwks_uri']:
            try:
                setattr(self, param, kwargs[param])
            except KeyError:
                pass
        self.jwx_def = {}
        self.build_jwx_def()
        self.ms_conf = None
        self.signers = None

    def build_jwx_def(self):
        self.jwx_def = {}

        for _typ in ["signing_alg", "encryption_alg", "encryption_enc"]:
            self.jwx_def[_typ] = {}
            for item in ["id_token", "userinfo"]:
                cap_param = '{}_{}_values_supported'.format(item, _typ)
                try:
                    self.jwx_def[_typ][item] = self.capabilities[cap_param][
                        0]
                except KeyError:
                    self.jwx_def[_typ][item] = ""

    def sign_encrypt_id_token(self, sinfo, client_info, areq, code=None,
                              access_token=None, user_info=None):
        # self._update_client_keys(client_info["client_id"])

        return provider.Provider.sign_encrypt_id_token(
            self, sinfo, client_info, areq, code, access_token, user_info)

    def no_kid_keys(self):
        keys = [copy.copy(k) for k in self.keyjar.get_signing_key()]
        for k in keys:
            k.kid = None
        return keys

    def create_fed_providerinfo(self, pcr_class=ProviderConfigurationResponse,
                            setup=None):
        _sms = []
        for spec in self.ms_conf:
            self.federation_entity.signer = self.signers[spec['signer']]
            try:
                fos = spec['federations']
            except KeyError:
                try:
                    fos = [spec['federation']]
                except KeyError:
                    fos = []

            _sms.append(self.create_signed_metadata_statement(fos))

        _response = provider.Provider.create_providerinfo(self, pcr_class,
                                                          setup)
        _response['metadata_statements'] = _sms
        return _response

    def _split_query(self, uri):
        base, query = splitquery(uri)
        if query:
            return base, parse_qs(query)
        else:
            return base, None

    def registration_endpoint(self, request, authn=None, **kwargs):
        try:
            reg_req = RegistrationRequest().deserialize(request, "json")
        except ValueError:
            reg_req = RegistrationRequest().deserialize(request)

        self.events.store(EV_PROTOCOL_REQUEST, reg_req)
        try:
            response_type_cmp(kwargs['test_cnf']['response_type'],
                              reg_req['response_types'])
        except KeyError:
            pass

        try:
            provider.Provider.verify_redirect_uris(reg_req)
        except InvalidRedirectURIError as err:
            return error(
                error="invalid_configuration_parameter",
                descr="Invalid redirect_uri: {}".format(err))

        # Do initial verification that all endpoints from the client uses
        #  https
        for endp in ["jwks_uri", "initiate_login_uri"]:
            try:
                uris = reg_req[endp]
            except KeyError:
                continue

            if not isinstance(uris, list):
                uris = [uris]
            for uri in uris:
                if not uri.startswith("https://"):
                    return error(
                        error="invalid_configuration_parameter",
                        descr="Non-HTTPS endpoint in '{}'".format(endp))

        _response = provider.Provider.registration_endpoint(self, request,
                                                            authn, **kwargs)
        self.events.store(EV_HTTP_RESPONSE, _response)
        self.init_keys = []
        if "jwks_uri" in reg_req:
            if _response.status == "200 OK":
                # find the client id
                req_resp = RegistrationResponse().from_json(
                    _response.message)
                for kb in self.keyjar[req_resp["client_id"]]:
                    if kb.imp_jwks:
                        self.events.store("Client JWKS", kb.imp_jwks)

        return _response

    def authorization_endpoint(self, request="", cookie=None, **kwargs):
        if isinstance(request, dict):
            _req = request
        else:
            _req = {}
            for key, val in parse_qs(request).items():
                if len(val) == 1:
                    _req[key] = val[0]
                else:
                    _req[key] = val

        # self.events.store(EV_REQUEST, _req)

        try:
            _scope = _req["scope"]
        except KeyError:
            return error(
                error="incorrect_behavior",
                descr="No scope parameter"
            )
        else:
            # verify that openid is among the scopes
            _scopes = _scope.split(" ")
            if "openid" not in _scopes:
                return error(
                    error="incorrect_behavior",
                    descr="Scope does not contain 'openid'"
                )

        client_id = _req["client_id"]

        try:
            f = response_type_cmp(self.capabilities['response_types_supported'],
                                  _req['response_type'])
        except KeyError:
            pass
        else:
            if f is False:
                self.events.store(
                    EV_FAULT,
                    'Wrong response type: {}'.format(_req['response_type']))
                return error_response(error="incorrect_behavior",
                                      descr="Not supported response_type")

        _rtypes = _req['response_type'].split(' ')

        if 'id_token' in _rtypes:
            try:
                self._update_client_keys(client_id)
            except TestError:
                return error(error="incorrect_behavior",
                             descr="No change in client keys")

        if isinstance(request, dict):
            request = urlencode(request)

        _response = provider.Provider.authorization_endpoint(self, request,
                                                             cookie,
                                                             **kwargs)

        if "rotenc" in self.behavior_type:  # Rollover encryption keys
            rsa_key = RSAKey(kid="rotated_rsa_{}".format(time.time()),
                             use="enc").load_key(RSA.generate(2048))
            ec_key = ECKey(kid="rotated_ec_{}".format(time.time()),
                           use="enc").load_key(P256)

            keys = [rsa_key.serialize(private=True),
                    ec_key.serialize(private=True)]
            new_keys = {"keys": keys}
            self.events.store("New encryption keys", new_keys)
            self.do_key_rollover(new_keys, "%d")
            self.events.store("Rotated encryption keys", '')
            logger.info(
                'Rotated OP enc keys, new set: {}'.format(
                    key_summary(self.keyjar, '')))

        # This is just for logging purposes
        try:
            _resp = self.server.http_request(_req["request_uri"])
        except KeyError:
            pass
        except requests.ConnectionError as err:
            self.events.store(EV_EXCEPTION, err)
            err = unwrap_exception(err)
            return error_response(error="server_error", descr=err)
        else:
            if _resp.status_code == 200:
                self.events.store(EV_REQUEST,
                                  "Request from request_uri: {}".format(
                                      _resp.text))

        return _response

    def token_endpoint(self, request="", authn=None, dtype='urlencoded',
                       **kwargs):
        try:
            req = AccessTokenRequest().deserialize(request, dtype)
            client_id = self.client_authn(self, req, authn)
        except Exception as err:
            logger.error(err)
            self.events.store(EV_EXCEPTION,
                              "Failed to verify client due to: %s" % err)
            return error(error="incorrect_behavior",
                         descr="Failed to verify client")

        try:
            self._update_client_keys(client_id)
        except TestError:
            logger.error('No change in client keys')
            return error(error="incorrect_behavior",
                         descr="No change in client keys")

        _response = provider.Provider.token_endpoint(self, request,
                                                     authn, dtype, **kwargs)

        return _response

    def generate_jwks(self, mode):
        if "nokid1jwk" in self.behavior_type:
            alg = mode["sign_alg"]
            if not alg:
                alg = "RS256"
            keys = [k.to_dict() for kb in self.keyjar[""] for k in
                    list(kb.keys())]
            for key in keys:
                if key["use"] == "sig" and key["kty"].startswith(alg[:2]):
                    key.pop("kid", None)
                    jwk = dict(keys=[key])
                    return json.dumps(jwk)
            raise Exception(
                "Did not find sig {} key for nokid1jwk test ".format(alg))
        else:  # Return all keys
            keys = [k.to_dict() for kb in self.keyjar[""] for k in
                    list(kb.keys())]
            jwks = dict(keys=keys)
            return json.dumps(jwks)

    def _update_client_keys(self, client_id):
        if "updkeys" in self.behavior_type:
            if not self.init_keys:
                if "rp-enc-key" in self.baseurl:
                    self.update_key_use = "enc"
                else:
                    self.update_key_use = "sig"
                self.init_keys = []
                for kb in self.keyjar[client_id]:
                    for key in kb.available_keys():
                        if isinstance(key, SYMKey):
                            pass
                        elif key.use == self.update_key_use:
                            self.init_keys.append(key)
            else:
                for kb in self.keyjar[client_id]:
                    self.events.store("Updating client keys")
                    kb.update()
                    if kb.imp_jwks:
                        self.events.store(
                            "Client JWKS", kb.imp_jwks)
                same = 0
                # Verify that the new keys are not the same
                for kb in self.keyjar[client_id]:
                    for key in kb.available_keys():
                        if isinstance(key, SYMKey):
                            pass
                        elif key.use == self.update_key_use:
                            if key in self.init_keys:
                                same += 1
                            else:
                                self.events.store('Key change',
                                                  "New key: {}".format(key))
                if same == len(self.init_keys):  # no change
                    self.events.store("No change in keys")
                    raise TestError("Keys unchanged")
                else:
                    self.events.store('Key change',
                                      "{} changed, {} the same".format(
                                          len(self.init_keys) - same, same))

    def __setattr__(self, key, value):
        if key == "keys":
            # Update the keyjar instead of just storing the keys description
            keyjar_init(self, value)
        else:
            super(provider.Provider, self).__setattr__(key, value)

    def _get_keyjar(self):
        return self.server.keyjar

    def _set_keyjar(self, item):
        self.server.keyjar = item

    keyjar = property(_get_keyjar, _set_keyjar)
