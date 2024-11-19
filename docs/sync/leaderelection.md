# Leader Election

It is using a distributed lock in order to guarantee that only one worker is the leader at any given time.

It needs to be started as a background task that runs indefinitely and is responsible for acquiring and renewing
the distributed lock. The lock is automatically released when the task is cancelled or stopped.
