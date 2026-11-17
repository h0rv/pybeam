from fastapi import HTTPException, Request


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = request.url.scheme + "://" + request.url.netloc
    if request.headers.get("sec-fetch-site") not in (None, "none", "same-origin") or (
        origin is not None and origin != expected
    ):
        raise HTTPException(403, "Cross-origin mutations are not allowed")
