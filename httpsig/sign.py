import base64
import six

from Crypto.Hash import HMAC
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

from .utils import *


class Signer(object):
    """
    When using an RSA algo, the secret is a PEM-encoded private key.
    When using an HMAC algo, the secret is the HMAC signing secret.
    
    Password-protected keyfiles are not supported.
    """
    def __init__(self, secret, algorithm='rsa-sha256'):
        assert algorithm in ALGORITHMS, "Unknown algorithm"
        if isinstance(secret, six.string_types): secret = secret.encode("ascii")
        
        self._rsa = None
        self._hash = None
        self.sign_algorithm, self.hash_algorithm = algorithm.split('-')
        
        if self.sign_algorithm == 'rsa':
            try:
                rsa_key = RSA.importKey(secret)
                self._rsa = PKCS1_v1_5.new(rsa_key)
                self._hash = HASHES[self.hash_algorithm]
            except ValueError:
                raise HttpSigException("Invalid key.")
            
        elif self.sign_algorithm == 'hmac':
            self._hash = HMAC.new(secret, digestmod=HASHES[self.hash_algorithm])

    @property
    def algorithm(self):
        return '%s-%s' % (self.sign_algorithm, self.hash_algorithm)

    def _sign_rsa(self, data):
        if isinstance(data, six.string_types): data = data.encode("ascii")
        h = self._hash.new()
        h.update(data)
        return self._rsa.sign(h)

    def _sign_hmac(self, data):
        if isinstance(data, six.string_types): data = data.encode("ascii")
        hmac = self._hash.copy()
        hmac.update(data)
        return hmac.digest()

    def _sign(self, data):
        if isinstance(data, six.string_types): data = data.encode("ascii")
        signed = None
        if self._rsa:
            signed = self._sign_rsa(data)
        elif self._hash:
            signed = self._sign_hmac(data)
        if not signed:
            raise SystemError('No valid encryptor found.')
        return base64.b64encode(signed).decode("ascii")


class HeaderSigner(Signer):
    '''
    Generic object that will sign headers as a dictionary using the http-signature scheme.
    https://github.com/joyent/node-http-signature/blob/master/http_signing.md

    key_id is the mandatory label indicating to the server which secret to use
    secret is the filename of a pem file in the case of rsa, a password string in the case of an hmac algorithm
    algorithm is one of the six specified algorithms
    headers is a list of http headers to be included in the signing string, defaulting to ['date'].
    '''
    def __init__(self, key_id, secret, algorithm='rsa-sha256', headers=None):
        super(HeaderSigner, self).__init__(secret=secret, algorithm=algorithm)
        self.headers = headers or ['date']
        self.signature_template = build_signature_template(key_id, algorithm, headers)

    def sign(self, headers, host=None, method=None, path=None):
        """
        Add Signature Authorization header to case-insensitive header dict.

        headers is a case-insensitive dict of mutable headers.
        host is a override for the 'host' header (defaults to value in headers).
        method is the HTTP method (required when using '(request-line)').
        path is the HTTP path (required when using '(request-line)').
        """
        headers = CaseInsensitiveDict(headers)
        required_headers = self.headers or ['date']
        signable = generate_message(required_headers, headers, host, method, path)
        
        signature = self._sign(signable)
        headers['authorization'] = self.signature_template % signature
        
        return headers

