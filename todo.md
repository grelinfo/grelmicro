❯ 1. Mutation-test resilience
     Run a mutmut campaign on the resilience module (circuit breaker, rate limiter, retry, shield) and close the genuine assertion gaps with verified killing tests, same proven flow as coordination. Highest robustness leverage before 1.0, the headline feature, never mutation-tested.
  2. Mutation-test cache
     Same campaign on the cache module (TTLCache, @cached, stampede, stale_ttl, tags). Complex, concurrency-heavy, never mutation-tested. Slightly lower stakes than resilience but real gaps likely.
  3. Finish coordination gaps
     Close the remaining ~7 mutation clusters from the original report (backend_timeout enforcement, metadata forwarding, last_confirmation_age sign, memory fencing-start). Completes what we started, smaller scope, diminishing returns.
  4. Launch-readiness sweep
     Audit LAUNCH_CHECKLIST.md and the comparison/capabilities pages for 1.0-final accuracy, verify every doc example runs against the published artifact, refresh any stale 'why not X' claims. Forward-looking toward 1.0 final and the launch.
  5. Type something.
