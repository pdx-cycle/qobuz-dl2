class AuthenticationError(Exception):
    pass


class IneligibleError(Exception):
    pass


class InvalidAppIdError(Exception):
    pass


class InvalidAppSecretError(Exception):
    pass


class InvalidQuality(Exception):
    pass


class NonStreamable(Exception):
    pass


class ResponseError(ValueError):
    """A service returned a response that cannot satisfy the requested contract."""


class StreamingEndpointRetiredError(RuntimeError):
    """Qobuz accepted authentication but retired this client's stream endpoint."""
