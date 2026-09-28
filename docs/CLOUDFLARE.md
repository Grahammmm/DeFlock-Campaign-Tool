# Cloudflare launch checklist

This is a manual deployment design, not an implemented installer.
The local build emits only a neutral public starter. Do not route production traffic
or open an admin surface until relevant checks have passed.

1. Select the organizer's Cloudflare account and domain; record resource identities.
2. Review current plan/usage costs before authorizing any purchase.
3. Preserve registrar and mail DNS: MX, SPF, DKIM, DMARC and verification records.
4. Deploy only the generated public directory with Workers Static Assets.
5. Keep private evidence in private R2 and structured state in D1, not asset folders.
6. Protect the workspace and backend routes with validated owner identity. Test anonymous
   denial and authorized access; never trust visitor-supplied identity headers.
7. Configure the canonical hostname and HTTP/HTTPS apex/www redirect matrix.
8. Add a provider-hosted signup form with the exact origin, a matching CSP, and CAPTCHA
   hostnames. Replace the starter's form-action/frame policy intentionally when integrating.
9. Use one approved test address to verify consent, welcome, list status and unsubscribe.
   Do not reactivate suppressed users during an import.
10. Keep analytics off rendering/signup critical paths; confirm actual stored events.
11. Test mobile, keyboard use, map loading/fallback, private routing and failure modes.
12. Run bounded first-party load with synthetic data and stub external services.
13. Record version, configuration, checks, open gaps and a compatible rollback.
14. Retain old deployment/data until cutover reconciliation is accepted.

Use current primary documentation:
- https://developers.cloudflare.com/workers/static-assets/
- https://developers.cloudflare.com/workers/platform/pricing/
- https://developers.cloudflare.com/d1/
- https://developers.cloudflare.com/r2/
- https://developers.cloudflare.com/cloudflare-one/access-controls/

Never publish cost or capacity guarantees from a single asset burst.
