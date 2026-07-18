# Outbox

- **Start here**: [Outbox guide](../outbox.md)
- **Common recipes**: [`publish`](../outbox.md#producer), [`@handler`](../outbox.md#consumer), [relay](../outbox.md#relay)
- **Configuration**: [Backend](../outbox.md#backend), [Configuration](../outbox.md#configuration)

::: grelmicro.outbox
    options:
      show_submodules: true
      members:
        - Outbox
        - OutboxBackend
        - OutboxConfig
        - Message
        - Retry
        - Cancel
        - OutboxError
        - OutboxHandleError
        - OutboxTransactionError
        - HandlerNotFoundError
        - HandlerAlreadyRegisteredError
