import {spawnSync} from 'node:child_process';
const run=args=>{const r=spawnSync('git',args,{encoding:'utf8'});if(r.status>1||r.error)throw Error('git scan failed');return r;};
const files=run(['ls-files','-z']).stdout.split('\0').filter(Boolean);
const forbidden=files.filter(p=>/(^|\/)(?:documents|records-project|private|\.runtime|\.wrangler|node_modules)(?:\/|$)|(^|\/)(?:\.env|\.dev\.vars)(?:\.|$)|\.(?:sqlite3?|db|pem|key|pdf|xlsx|eml|zip)$/.test(p));
const patterns=['-----BEGIN ([A-Z ]+ )?PRIVATE KEY-----','(gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}','xkeysib-[A-Za-z0-9_-]{20,}','(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{30,}','AKIA[A-Z0-9]{16}','[?&](X-Amz-Signature|X-Amz-Credential|access_token|api_key)=','https://[a-z0-9-]+\\.cloudflareaccess\\.com','["\x27](ADMIN_)?ACCESS_(AUD|SUBJECTS|JWKS|ISSUER)["\x27][[:space:]]*:[[:space:]]*["\x27][^<]'];
let failed=forbidden.length>0;
if(forbidden.length)console.error('Forbidden tracked paths: '+forbidden.join(', '));
for(const pattern of patterns){const r=run(['grep','--cached','-I','-l','-E','-e',pattern,'--','.']);if(r.status===0){failed=true;console.error('Secret/private-setting signature in: '+r.stdout.trim());}}
const keywords=run(['grep','--cached','-I','-l','-i','-E','secret|token|password|api[_-]?key','--','.']).stdout.trim().split('\n').filter(Boolean);
console.log(JSON.stringify({trackedFiles:files.length,keywordReviewFiles:keywords,credentialValuesPrinted:false,forbiddenPathCount:forbidden.length}));
if(failed)process.exit(1);
console.log('PASS: staged Git content contains no detected credential signatures, Access values, or forbidden storage paths. Names/placeholders require human review; this is not a guarantee against every secret format.');
