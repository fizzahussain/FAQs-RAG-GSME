"""Google Workspace verification. No homemade JWT validation or email trust."""

from .access import Principal


class IdentityUnavailable(RuntimeError):
    pass


class InvalidIdentity(ValueError):
    pass


def google_principal(token, client_id, domains):
    if not client_id or not domains:
        raise IdentityUnavailable("Google Workspace sign-in is not configured")
    if not token or len(token) > 16000:
        raise InvalidIdentity("Sign-in required")
    try:
        from google.oauth2 import id_token
        from google.auth.transport.requests import Request
        from google.auth.exceptions import TransportError
    except ImportError:
        raise IdentityUnavailable("Install the hosted dependency extra") from None
    try:
        transport = Request()
        def bounded_request(*args, **kwargs):
            kwargs["timeout"] = 5
            return transport(*args, **kwargs)
        claims = id_token.verify_oauth2_token(token, bounded_request, client_id)
        if (claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
                or claims.get("aud") != client_id or claims.get("email_verified") is not True
                or claims.get("hd", "").casefold() not in domains
                or not isinstance(claims.get("sub"), str) or not claims["sub"].strip()):
            raise InvalidIdentity("Use an authorized company Google Workspace account")
        # Google sub is stable; the email can change and is not the owner key.
        return Principal("google:" + claims["sub"])
    except InvalidIdentity:
        raise
    except TransportError:
        raise IdentityUnavailable("Google verification service is unavailable") from None
    except Exception:
        raise InvalidIdentity("Google sign-in could not be verified") from None
