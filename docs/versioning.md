# Versions and upgrades

grelmicro is in `0.x`. Every release says in the [changelog](changelog.md) what
changes for you, and this page says what each kind of release may change.

## Pin the minor

Pin grelmicro to the minor you use:

```toml
dependencies = ["grelmicro~=0.44.0"]
```

`~=0.44.0` takes every `0.44.x` release and never `0.45.0`. Every upgrade it
allows is safe. When you are ready for the next minor, read its section of the
[migration guide](migration.md), run your tests, and move the pin.

## Patch releases

A patch release, such as `0.44.1`, brings fixes and new features. It never
removes anything, never changes a default or a behavior you rely on, and never
starts raising an error or a warning. Upgrade without reading.

## Minor releases

A minor release, such as `0.45.0`, may break your code. Every break is listed
under **Breaking** in the [changelog](changelog.md), with what to change in the
[migration guide](migration.md).

A fix for wrong behavior or a security fix may break at once, in the next
minor. A removed or renamed name fails at import, so you see it right away.

## Changed defaults warn first

A change you could miss, such as a new default or a value that now reads
differently, warns before it ships. The warning shows at the line that uses
it, and names what changes, the release it lands in, and the line to change.

The change ships at least 4 weeks after the release that first warned. Until
then, your code keeps working as it does today. This window grows as the API
settles, and never shrinks.

Each release that switches has its own warning class, such as
`Grelmicro045Warning` for `0.45.0`. Turn it into an error in your tests to find
every line to change before you upgrade:

```toml
[tool.pytest.ini_options]
filterwarnings = ["error::grelmicro.Grelmicro045Warning"]
```

## Python versions

grelmicro supports up to three Python versions at once, counted back from the
newest stable release. A new Python is supported when it is released. A version
is dropped only when it is no longer one of the three, and only in a minor.

Today grelmicro supports Python 3.14 and 3.15. Python 3.14 stays supported at
least until Python 3.17 is released.

## After 1.0

From `1.0`, nothing breaks before `2.0`. `1.0` comes when the API has held
still, not on a date.
