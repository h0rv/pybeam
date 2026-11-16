try:
    from erlang_services import ServiceBusyError as ServiceBusyError
    from erlang_services import ServiceRef as ServiceRef
    from erlang_services import ServiceSlot as ServiceSlot
    from erlang_services import ServiceState as ServiceState
except ImportError as error:
    raise RuntimeError("Native background services are required; no Python fallback") from error
