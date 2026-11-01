# Task

- **Start here**: [Task Scheduler guide](../task.md)
- **Common recipes**: [`Tasks`](../task.md#tasks), [`Interval Task`](../task.md#interval-task), [`On-demand jobs`](../task.md#on-demand-jobs), [`TaskRouter`](../task.md#task-router)
- **Cluster-wide**: gate per-process tasks with [`TaskLock`](../coordination/task-lock.md) or [`LeaderElection`](../coordination/leader-election.md).

::: grelmicro.task
    options:
      show_submodules: true
      members:
        - TaskError
        - Tasks
        - TasksConfig
        - TaskRouter
        - Job
        - OwnedJob
        - Progress
        - JobInfo
        - JobStatus
        - JobError
        - JobRefusedError
