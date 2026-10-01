"""Host-image attestation is trusted verification, never a record boolean."""
import hashlib
import json
from .core import private_path

def assess(profile,runtime,trusted_verifier=None):
    spec=profile.get('image_attestation')
    if not isinstance(spec,dict):return {'state':'declared_only','blocked_reason':'independent_image_attestation_missing'}
    try:
        path=private_path(spec['path'])
        with path.open('rb') as f:raw=f.read(65537)
        if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=spec['sha256']:raise ValueError('attestation hash')
        body=json.loads(raw)
        if body.get('image_digest')!=runtime['image_digest'] or body.get('code_sha256')!=runtime['code_sha256']:raise ValueError('attestation binding')
        if not callable(trusted_verifier) or trusted_verifier(raw,runtime,spec.get('verifier_id')) is not True:
            return {'state':'unverified','blocked_reason':'trusted_image_verifier_unavailable_or_rejected'}
        return {'state':'independently_verified','attestation_sha256':spec['sha256'],'verifier_id':spec.get('verifier_id')}
    except (OSError,ValueError,KeyError,TypeError):return {'state':'unverified','blocked_reason':'image_attestation_invalid'}
