#!/usr/bin/env python3
import argparse
import json

import requests
from fedoidc import ProviderConfigurationResponse
from fedoidc.bundle import JWKSBundle
from fedoidc.client import Client
from fedoidc.entity import FederationEntity
from oic.exception import ParameterError
from oic.exception import RegistrationError
from oic.utils.keyio import KeyJar
from oic.utils.keyio import build_keyjar
from otest.flow import Flow
from requests.packages import urllib3

urllib3.disable_warnings()

# ----- config -------
#tool_url = "https://agaton-sax.com:8080"
tool_url = "https://localhost:8080"
tester = 'dummy'
KEY_DEFS = [
    {"type": "RSA", "key": '', "use": ["sig"]},
]


def fo_jb(jb, test_info):
    fjb = JWKSBundle('')
    try:
        _vals = test_info['metadata_statements']
    except KeyError:
        _vals = test_info['metadata_statement_uris']

    for ms in _vals:
        try:
            for fo in ms['federations']:
                fjb[fo] = jb[fo]
        except KeyError:
            try:
                fo = ms['federation']
                fjb[fo] = jb[fo]
            except KeyError:
                fo = ms['signer']
                fjb[fo] = jb[fo]

    return fjb


def run_test(test_id, tool_url, tester, rp_fed_ent, jb):
    _iss = "{}/{}/{}".format(tool_url, tester, test_id)
    _url = "{}/{}".format(_iss, ".well-known/openid-configuration")
    resp = requests.request('GET', _url, verify=False)

    rp_fed_ent.jwks_bundle = fo_jb(jb, _flows[test_id])
    rp = Client(federation_entity=rp_fed_ent, verify_ssl=False)
    rp_fed_ent.httpcli = rp

    # Will raise an exception if there is no metadata statement I can use
    try:
        rp.handle_response(resp, _iss, rp.parse_federation_provider_info,
                           ProviderConfigurationResponse)
    except (RegistrationError, ParameterError) as err:
        print(test_id, "Exception: {}".format(err))
        return

    # If there are more the one metadata statement I can use
    # provider_federations will be set and will contain a dictionary
    # keyed on FO identifier
    if rp.provider_federations:
        print(test_id, [p.fo for p in rp.provider_federations])
    else:  # Otherwise there should be exactly one metadata statement I can use
        print(test_id, rp.federation)

# --------------------

parser = argparse.ArgumentParser()
parser.add_argument('-f', dest='flowsdir', required=True)
parser.add_argument('-k', dest='insecure', action='store_true')
parser.add_argument('-t', dest='test_id')
#parser.add_argument(dest="config")
args = parser.parse_args()

_flows = Flow(args.flowsdir, profile_handler=None)
tests = _flows.keys()

# Get the necessary information about the JWKS bundle
info = {}
for path in ['bundle', 'bundle/signer', 'bundle/sigkey']:
    _url = "{}/{}".format(tool_url, path)
    resp = requests.request('GET', _url, verify=False)
    info[path] = resp.text

# Create a KeyJar instance that contains the key that the bundle was signed with
kj = KeyJar()
kj.import_jwks(json.loads(info['bundle/sigkey']), info['bundle/signer'])

# Create a JWKSBundle instance and load it with the keys in the bundle
# I got from the tool
jb = JWKSBundle('')
jb.upload_signed_bundle(info['bundle'], kj)

# This is for the federation entity to use when signing something
# like the keys at jwks_uri.
_kj = build_keyjar(KEY_DEFS)[1]

# A federation aware RP includes a FederationEntity instance.
# This is where it is instantiated
rp_fed_ent = FederationEntity(None, keyjar=_kj, iss='https://sunet.se/rp',
                              signer=None, fo_bundle=None)

# And now for running the tests
if args.test_id:
    run_test(args.test_id, tool_url, tester, rp_fed_ent, jb)
else:
    for test_id in tests:
        run_test(test_id, tool_url, tester, rp_fed_ent, jb)
