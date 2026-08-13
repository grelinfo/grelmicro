# Outbox

- **Start here**: [Outbox guide](../outbox/index.md)
- **Common recipes**: [`publish`](../outbox/producer.md), [`@handler`](../outbox/consumer.md), [relay](../outbox/relay.md)
- **Configuration**: [Backend](../outbox/index.md#backend), [Configuration](../outbox/relay.md#configuration)

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
