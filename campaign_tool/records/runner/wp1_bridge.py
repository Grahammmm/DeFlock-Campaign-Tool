"""Narrow trusted startup registration; receipt data never chooses validators."""
from .contracts import IntegrationGap
from .core import hid


def preservation_profile(database, output, identity, *, binding_context=None):
    """Deterministic identity only; this neither registers nor grants authority."""
    binding=hid([str(database),str(output)] + ([] if binding_context is None else [binding_context]))
    adapter_id='wp2-mail-cas-preserver-v3:'+binding
    profile='wp2-mail-preserve:'+hid([binding,identity['runtime']['version'],identity['config_sha256']])
    return adapter_id,profile


def installed_preservation_runner(stages,database,identity,validator,*,binding_context=None,preservation_context=None):
    """Installed code only. Domain validator is the mail backend's real verifier.

    WP1 has no public callable-registration submission API. Its installed registry
    is initialized here by pinned WP2 startup code, never by a record/CLI payload.
    No downstream adapter or generic successful callback is registered.
    """
    registry=getattr(stages,'_INSTALLED_VALIDATORS',None)
    if not isinstance(registry,dict):raise IntegrationGap('wp1_installed_registry_unavailable')
    if preservation_context is not None:
        binding_context=hid([binding_context,preservation_context.fingerprint])
    adapter_id,profile=preservation_profile(database,validator.__self__.output,identity,binding_context=binding_context)
    previous=registry.get(adapter_id)
    if previous is not None:
        owner=getattr(previous,'__self__',None)
        if owner is None or owner.database!=database or owner.output!=validator.__self__.output:
            raise IntegrationGap('wp1_installed_registry_binding_conflict')
    else:registry[adapter_id]=validator
    runtime=identity['runtime']
    stages.configure_installed_profile(profile,engine_version=runtime['version'],
        config_sha256=identity['config_sha256'],validators={'preserve':adapter_id})
    kwargs={} if preservation_context is None else {'preservation_context':preservation_context}
    return stages.installed_runner(database,run_id=identity['run_id'],owner='runner-preserver',profile_id=profile,**kwargs)
