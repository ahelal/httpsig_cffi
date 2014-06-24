from datetime import datetime
from getpass import getpass
from os.path import expanduser
from time import mktime
from wsgiref.handlers import format_date_time
import base64

from Crypto.Hash import SHA256, SHA, SHA512, HMAC
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

from .utils import sig, is_rsa, CaseInsensitiveDict

ALGORITHMS = frozenset(['rsa-sha1', 'rsa-sha256', 'rsa-sha512', 'hmac-sha1', 'hmac-sha256', 'hmac-sha512'])
HASHES = {'sha1':   SHA,
          'sha256': SHA256,
          'sha512': SHA512}

class Signer(object):
    def __init__(self, secret='~/.ssh/id_rsa', algorithm='rsa-sha256'):
        assert algorithm in ALGORITHMS, "Unknown algorithm"
        self._rsa = False
        self._hash = None
        self.sign_algorithm, self.hash_algorithm = algorithm.split('-')
        if self.sign_algorithm == 'rsa':
            self._rsa = self._get_key(secret)
            self._hash = HASHES[self.hash_algorithm]
        elif self.sign_algorithm == 'hmac':
            self._hash = HMAC.new(secret, digestmod=HASHES[self.hash_algorithm])

    @property
    def algorithm(self):
        return '%s-%s' % (self.sign_algorithm, self.hash_algorithm)

    def _get_key(self, secret):
        if (secret.startswith('-----BEGIN RSA PRIVATE KEY-----') or
            secret.startswith('-----BEGIN PRIVATE KEY-----')):
            # string with PEM encoded key data
            k = secret
        else:
            # file with key data
            with open(expanduser(secret)) as fh:
                k = fh.read()
        try:
            rsa_key = RSA.importKey(k)
        except ValueError:
            pw = getpass('RSA SSH Key Password: ')
            rsa_key = RSA.importKey(k, pw)
        return PKCS1_v1_5.new(rsa_key)

    def _sign_rsa(self, sign_string):
        h = self._hash.new()
        h.update(sign_string)
        return self._rsa.sign(h)

    def _sign_hmac(self, sign_string):
        hmac = self._hash.copy()
        hmac.update(sign_string)
        return hmac.digest()


    def _sign(self, sign_string):
        data = None
        if self._rsa:
            data = self._sign_rsa(sign_string)
        elif self._hash:
            data = self._sign_hmac(sign_string)
        if not data:
            raise SystemError('No valid encryption: try allow_agent=False ?')
        return base64.b64encode(data)


class AgentSigner(Signer):
    def __init__(self, secret='~/.ssh/id_rsa', algorithm='rsa-sha256'):
        super(AgentSigner, self).__init__()
        self._agent_key = False

    def _get_key(self):
        try:
            import paramiko as ssh
        except ImportError:
            import ssh
        keys = ssh.Agent().get_keys()
        self._keys = filter(is_rsa, keys)
        if self._keys:
            self._agent_key = self._keys[0]
            self._keys = self._keys[1:]
            self.sign_algorithm, self.hash_algorithm = ('rsa', 'sha1')

    def swap_keys(self):
        if self._keys:
            self._agent_key = self._keys[0]
            self._keys = self._keys[1:]
        else:
            self._agent_key = None

    def sign_agent(self, sign_string):
        data = self._agent_key.sign_ssh_data(None, sign_string)
        return sig(data)

    def sign(self, sign_string):
        data = self.sign_agent(sign_string)
        return base64.b64encode(data)


class HeaderSigner(Signer):
    '''
    Generic object that will sign headers as a dictionary using the http-signature scheme.
    https://github.com/joyent/node-http-signature/blob/master/http_signing.md

    key_id is the mandatory label indicating to the server which secret to use
    secret is the filename of a pem file in the case of rsa, a password string in the case of an hmac algorithm
    algorithm is one of the six specified algorithms
    headers is a list of http headers to be included in the signing string, defaulting to "Date" alone.
    '''
    def __init__(self, key_id='', secret='~/.ssh/id_rsa', algorithm='rsa-sha256', headers=None):
        
        #PyCrypto wants strings, not unicode. We're not so demanding as an API.
        key_id = str(key_id)
        secret = str(secret)
        
        super(HeaderSigner, self).__init__(secret=secret, algorithm=algorithm)
        self.headers = headers
        self.signature_template = self.build_signature_template(key_id, algorithm, headers)

    def build_signature_template(self, key_id, algorithm, headers):
        """
        Build the Signature template for use with the Authorization header.

        key_id is the mandatory label indicating to the server which secret to use
        algorithm is one of the six specified algorithms
        headers is a list of http headers to be included in the signing string.

        The signature must be interpolated into the template to get the final Authorization header value.
        """
        param_map = {'keyId': key_id,
                     'algorithm': algorithm,
                     'signature': '%s'}
        if headers:
            headers = [h.lower() for h in headers]
            param_map['headers'] = ' '.join(headers)
        kv = map('{0[0]}="{0[1]}"'.format, param_map.items())
        kv_string = ','.join(kv)
        sig_string = 'Signature {0}'.format(kv_string)
        return sig_string

    def sign(self, headers, host=None, method=None, path=None):
        """
        Add Signature Authorization header to case-insensitive header dict.

        headers is a case-insensitive dict of mutable headers.
        host is a override for the 'host' header (defaults to value in headers).
        method is the HTTP method (used for '(request-line)').
        path is the HTTP path (used for '(request-line)').
        """
        headers = CaseInsensitiveDict(headers)
        
        # AK: Possible problem here if the client and server's dates are off
        #     by even one second, this will fail miserably.  This is also not
        #     in the spec.  Should probably be removed.
        # if 'date' not in headers:
        #     now = datetime.now()
        #     stamp = mktime(now.timetuple())
        #     headers['date'] = format_date_time(stamp)
        
        required_headers = self.headers or ['date']
        signable_list = []
        for h in required_headers:
            if h == '(request-line)':
                if not method or not path:
                    raise Exception('method and path arguments required when using "(request-line)"')
                signable_list.append('%s %s' % (method.lower(), path))

            elif h == 'host':
                # 'host' special case due to requests lib restrictions
                # 'host' is not available when adding auth so must use a param
                # if no param used, defaults back to the 'host' header
                if not host:
                    if 'host' in headers:
                        host = headers[h]
                    else:
                        raise Exception('missing required header "%s"' % (h))
                signable_list.append('%s: %s' % (h.lower(), host))
            else:
                if h not in headers:
                    raise Exception('missing required header "%s"' % (h))

                signable_list.append('%s: %s' % (h.lower(), headers[h]))

        signable = '\n'.join(signable_list)
        signature = self._sign(signable)
        headers['Authorization'] = self.signature_template % signature

        return headers
