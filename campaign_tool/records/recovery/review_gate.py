"""Trusted-startup WP8 bridge. Never infer review from opaque bytes or a callback."""
from contextlib import contextmanager
from .backup import canonical,sha

FIELDS=frozenset(('schema','bundle_id','finding_digest','public_content_sha256',
                  'review_set_sha256','owner_receipt_id','authority_policy_sha256'))
SCHEMA='wp8-publication-reference-v1'


class ReviewGateError(ValueError):pass


def require(value,reason):
    if not value:raise ReviewGateError(reason)


class WP8ReviewGate:
    """Only construct at trusted startup with the installed WP8 Authority.

    Uses WP8's pinned locked-assessment helpers to retain the same root lock
    across assessment and the outbox action. WP8 remains a separate dependency.
    """
    def __init__(self,*,root,authority):
        from campaign_tool.records import review_bundle as wp8
        require(type(authority) is wp8.Authority,'installed_wp8_authority_required')
        self.wp8=wp8;self.root=wp8._root(root);self.authority=authority
        self.test_only=authority.test_only

    def _checked(self,root,bundle_id,owner_receipt_id,owner):
        wp8=self.wp8
        report=wp8._assess(root,bundle_id,self.authority)
        directory,body,public=wp8._load(root,bundle_id,self.authority)
        require(report['review_complete'] is True and not report['blockers'],'wp8_current_review_blocked')
        require(report['owner_approval']=='approved','wp8_authenticated_owner_approval_required')
        owners=dict(wp8._entries(directory/'owners',self.authority))
        decision=owners.get(owner_receipt_id)
        require(decision is not None and decision['kind']=='owner','wp8_owner_receipt_missing')
        require(decision['actor']['identity']==owner and 'owner' in decision['actor']['roles'],'wp8_owner_identity_mismatch')
        payload=decision['payload']
        require(payload['decision']=='approve' and all(payload[k]==report[k] for k in
                ('bundle_id','finding_digest','public_content_sha256','review_set_sha256')),'wp8_owner_decision_stale')
        return report,body,public

    def reference(self,bundle_id,*,owner_receipt_id,owner):
        """Bind the current exact review set and authenticated owner decision."""
        try:
            with self.wp8._locked(self.root) as root:
                report,body,public=self._checked(root,bundle_id,owner_receipt_id,owner)
                return canonical({'schema':SCHEMA,**{k:report[k] for k in
                    ('bundle_id','finding_digest','public_content_sha256','review_set_sha256')},
                    'owner_receipt_id':owner_receipt_id,'authority_policy_sha256':self.authority.policy_sha256})
        except (self.wp8.BundleError,OSError) as exc:
            raise ReviewGateError('wp8_assessment_unavailable') from exc

    @contextmanager
    def assess(self,job,*,owner,test_only):
        wp8=self.wp8
        require(not self.test_only or test_only is True,'synthetic_wp8_not_production')
        try:
            ref=wp8.decode(job['review_bundle'])
            require(type(ref) is dict and set(ref)==FIELDS and ref['schema']==SCHEMA,'wp8_reference_schema')
            require(sha(job['review_bundle'])==job['review_bundle_sha256'],'wp8_reference_hash_changed')
            require(ref['authority_policy_sha256']==self.authority.policy_sha256,'wp8_authority_policy_changed')
        except (wp8.BundleError,OSError) as exc:
            raise ReviewGateError('wp8_reference_invalid') from exc
        # The lock is held until the outbox action/ack completes. Authenticated
        # review/owner appends through WP8 cannot race assessment and deployment.
        try:
            lock=wp8._locked(self.root)
            root=lock.__enter__()
        except (wp8.BundleError,OSError) as exc:
            raise ReviewGateError('wp8_review_lock_unavailable') from exc
        try:
            try:
                report,body,public=self._checked(root,ref['bundle_id'],ref['owner_receipt_id'],owner)
                require(all(ref[k]==report[k] for k in ('bundle_id','finding_digest','public_content_sha256','review_set_sha256')),'wp8_review_set_or_content_changed')
                require(public==job['content'] and sha(public)==job['public_content_sha256'],'wp8_public_bytes_mismatch')
                proposal=body['proposal']
                require(proposal['proposal_id']==job['proposal_id'],'wp8_proposal_identity_mismatch')
                if proposal['supersedes'] is None:
                    require(job['supersedes'] is None,'wp8_supersession_mismatch')
                else:
                    _,prior,_=wp8._load(root,proposal['supersedes'],self.authority)
                    require(prior['proposal']['proposal_id']==job['supersedes'],'wp8_supersession_mismatch')
                if not test_only:
                    require(report['production_review_complete'] is True and report['handoff_ready'] is True,'wp8_production_handoff_not_ready')
                assessment={'bundle_id':ref['bundle_id'],'review_set_sha256':ref['review_set_sha256'],
                            'owner_receipt_id':ref['owner_receipt_id'],'test_only':self.test_only,
                            'production_review_complete':report['production_review_complete']}
            except (wp8.BundleError,OSError) as exc:
                raise ReviewGateError('wp8_assessment_unavailable') from exc
            yield assessment
        finally:
            lock.__exit__(None,None,None)
