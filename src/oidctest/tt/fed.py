import cherrypy
import json

from fedoidc import MetadataStatement, ProviderConfigurationResponse
from fedoidc.client import Client
from fedoidc.entity import FederationEntity
from fedoidc.operator import Operator

from jwkest import as_bytes, as_unicode
from oic.exception import RegistrationError, ParameterError

from oic.oauth2 import MessageException
from oic.oauth2 import VerificationError
from oic.utils.keyio import KeyJar


class Who(object):
    def __init__(self, fos):
        self.fos = fos

    @cherrypy.expose
    def index(self):
        cherrypy.response.headers['Content-Type'] = 'application/json'
        return as_bytes(json.dumps(list(self.fos.values())))


class Sign(object):
    def __init__(self, signer):
        self.signer = signer

    @cherrypy.expose
    def index(self, signer='', **kwargs):
        if not signer:
            raise cherrypy.HTTPError(400, 'Missing signer')
        if signer not in self.signer:
            raise cherrypy.HTTPError(400, 'unknown signer')

        if cherrypy.request.process_request_body is True:
            _json_doc = cherrypy.request.body.read()
        else:
            raise cherrypy.HTTPError(400, 'Missing Client registration body')

        if _json_doc == b'':
            raise cherrypy.HTTPError(400, 'Missing Client registration body')

        _args = json.loads(as_unicode(_json_doc))
        _mds = MetadataStatement(**_args)

        try:
            _mds.verify()
        except (MessageException, VerificationError) as err:
            raise cherrypy.CherryPyException(str(err))
        else:
            _sign = self.signer[signer]
            _jwt = _sign.create_signed_metadata_statement(_mds)
            cherrypy.response.headers['Content-Type'] = 'application/jwt'
            return as_bytes(_jwt)


class FoKeys(object):
    def __init__(self, bundle):
        self.bundle = bundle

    @cherrypy.expose
    def index(self, iss=''):
        cherrypy.response.headers['Content-Type'] = 'application/jwt'
        if iss:
            if isinstance(iss, list):
                return as_bytes(self.bundle.create_signed_bundle(iss_list=iss))
            else:
                return as_bytes(
                    self.bundle.create_signed_bundle(iss_list=[iss]))
        else:
            return as_bytes(self.bundle.create_signed_bundle())

    @cherrypy.expose
    def sigkey(self):
        cherrypy.response.headers['Content-Type'] = 'application/json'
        return as_bytes(json.dumps(self.bundle.sign_keys.export_jwks()))

    @cherrypy.expose
    def signer(self):
        return as_bytes(self.bundle.iss)


class Verify(object):
    @cherrypy.expose
    def index(self, iss, jwks, ms, **kwargs):
        _kj = KeyJar()
        _kj.import_jwks(json.loads(jwks), iss)
        op = Operator()

        try:
            _ms = op.unpack_metadata_statement(jwt_ms=ms, keyjar=_kj,
                                               cls=MetadataStatement)
            response = json.dumps(_ms.to_dict(), sort_keys=True, indent=2,
                                  separators=(',', ': '))
            cherrypy.response.headers['Content-Type'] = 'text/plain'
            return as_bytes(response)
        except (RegistrationError, ParameterError):
            raise cherrypy.HTTPError(400, b'Invalid Metadata statement')


def named_kc(config, iss):
    _kc = config.KEYDEFS[:]
    for kd in _kc:
        if 'key' in kd:
            kd['key'] = iss
    return _kc