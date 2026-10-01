"""Narrow trusted startup registration; receipt data never chooses validators."""
from .contracts import IntegrationGap
from .core import hid


def installed_preservation_runner(stages,database,identity,validator):
    """Installed code only. Domain validator is the mail backend's real verifier.

    WP1 has no public callable-registration submission API. Its installed registry
    is initialized here by pinned WP2 startup code, never by a record/CLI payload.
    No downstream adapter or generic successful callback is registered.
    """
    registry=getattr(stages,'_INSTALLED_VALIDATORS',None)
    if not isinstance(registry,dict):raise IntegrationGap('wp1_installed_registry_unavailable')
    binding=hid([str(database),str(validator.__self__.output)])
    adapter_id='wp2-mail-cas-preserver-v3:'+binding
    previous=registry.get(adapter_id)
    if previous is not None:
        owner=getattr(previous,'__self__',None)
        if owner is None or owner.database!=database or owner.output!=validator.__self__.output:
            raise IntegrationGap('wp1_installed_registry_binding_conflict')
    else:registry[adapter_id]=validator
    runtime=identity['runtime'];profile='wp2-mail-preserve:'+hid([binding,runtime['version'],identity['config_sha256']])
    stages.configure_installed_profile(profile,engine_version=runtime['version'],
        config_sha256=identity['config_sha256'],validators={'preserve':adapter_id})
    return stages.installed_runner(database,run_id=identity['run_id'],owner='runner-preserver',profile_id=profile)
