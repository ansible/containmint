Changelog
=========

0.6.0 - 2025-09-10
------------------

* Update CI to use Python 3.13.
* Update RHEL 9.4 tests to 9.5.
* Add RHEL 10.0 tests.
* Remove Ubuntu 22.04 tests.
* Update ``pyproject.toml`` to use PEP 639 license metadata.
* Pin ``ansible-core`` dependency to version 2.19.2.
* Use ``flit_core==3.12.0`` for build.
* Switch Ubuntu builds from Docker to Podman.
* Change default remote image to ``ubuntu/24.04``.
* Update test requirements.
* Update integration tests to use Podman locally.

0.5.1 - 2025-01-10
------------------

* Use ``flit_core==3.10.1`` for build.
* Remove Python classifiers from project metadata.

0.5.0 - 2025-01-10
------------------

* Pin ``ansible-core`` dependency to version 2.18.1.
* Require Python 3.11 - 3.13.
* Change default remote image to ``ubuntu/22.04``.
  This was the default in 0.2.0, which is still the most common and stable use case.

0.4.0 - 2022-08-05
------------------

* Add support for ``--build-arg`` passthrough to container builds.

0.3.0 - 2022-06-27
------------------

* Add support for ``--squash={all|new}`` to squash image layers (podman only).
* Change default remote image to ``rhel/9.0`` (so squashing works by default).

0.2.0 - 2022-06-21
------------------

* Pin ``ansible-core`` dependency to version 2.13.1.

0.1.0 - 2022-05-13
------------------

* Initial release.
