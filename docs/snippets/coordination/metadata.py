from grelmicro.coordination import LeaderElection

leader = LeaderElection(
    "worker",
    metadata={"pod": "web-1", "version": "1.4.0"},
)


def report() -> None:
    record = leader.record
    if record is not None:
        print(f"leader is {record.holder} ({record.metadata})")
        print(
            f"held since {record.acquired_at}, {record.transitions} handovers"
        )
