containmint
===========

Create multi-arch containers using native cloud builds.

Q&A
===

Why another tool?
-----------------

Most existing tools and services rely on QEMU to perform container builds on other architectures.
These builds are much slower, often running 15x longer than native builds.

Additionally, using customizable virtual machines allows for builds which dedicated build services may not support.

How does it work?
-----------------

Ephemeral virtual machines are provisioned through the Ansible Core CI service [#ansible_core_ci]_.
These virtual machines are used to perform native container builds.
The resulting images are pushed to a container registry.

After the container images are pushed, a manifest list referencing the container images is created.
The manifest list is then pushed to a container registry.

.. rubric:: Footnotes

.. [#ansible_core_ci] Authentication is required.
   An API key must be provided, or the tool must be run from an approved organization at a supported CI provider.

Usage Examples
==============

Configure container registry credentials
----------------------------------------

The credentials [#no_login]_ for the container registry [#one_registry]_ are set using environment variables:

.. code-block::

   export CONTAINMINT_USERNAME = 'my-username'
   export CONTAINMINT_PASSWORD = 'my-password'

.. rubric:: Footnotes

.. [#no_login] Use the ``--no-login`` option to allow operation without credentials.
   This option is only usable when not pushing to a container registry.

.. [#one_registry] Only one container registry can be used with each invocation.
   Multiple repositories from the same registry can be used.

Build and push a multi-arch container
-------------------------------------

The following steps can be performed in parallel:

.. code-block::

   containmint build --push --tag quay.io/my_org/scratch_repo:my_tag-x86_64 --arch x86_64
   containmint build --push --tag quay.io/my_org/scratch_repo:my_tag-aarch64 --arch aarch64

Once the steps above have been completed:

.. code-block::

   containmint merge --push \
     --tag quay.io/my_org/final_repo:my_tag \
           quay.io/my_org/scratch_repo:my_tag-x86_64 \
           quay.io/my_org/scratch_repo:my_tag-aarch64

This results in three tags:

* ``quay.io/my_org/final_repo:my_tag`` -- This manifest list contains x86_64 and aarch64 images.
* ``quay.io/my_org/scratch_repo:my_tag-x86_64`` -- This image is x86_64 only.
* ``quay.io/my_org/scratch_repo:my_tag-aarch64`` -- This image is aarch64 only.
