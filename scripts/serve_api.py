from __future__ import annotations


def main() -> None:
    import uvicorn

    from common.config import get_settings
    from common.network import validate_network_exposure

    settings = get_settings()
    validate_network_exposure(settings)
    uvicorn.run("api.main:app", host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
