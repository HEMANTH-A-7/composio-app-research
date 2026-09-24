## Field rubric (apply exactly)

- primary_auth: the credential a *multi-tenant agent toolkit* would use to act for many end users. If OAuth2 is offered for third-party apps, it is oauth2. If only per-account keys/tokens exist, api_key (a static key/secret) or bearer_token (a personal/access token the user generates). Bot tokens (Telegram, Discord bot) = bearer_token. Twilio Account SID+Auth Token over HTTP Basic = basic. A local CLI/library with no auth = none.
- access:
  - self_serve_free: a developer can sign up and get working credentials at $0 with no human approval (free tier, free dev account, free sandbox).
  - self_serve_trial: credentials only via a time-limited free trial of a paid product.
  - paid_plan_required: API access needs a paid plan/credits (a trial alone does not unlock API, or API is on a higher tier only).
  - approval_required: self-serve signup, but production use needs app review / developer token approval / verification by the vendor (e.g. Google Ads developer token, Meta app review, Amazon SP-API registration).
  - partner_or_sales_gated: credentials only via a partner program, contact-sales, or an enterprise contract.
  - no_public_api: no documented public API.
- verdict: build_now = a developer can build and test a working toolkit today for free; build_with_friction = buildable but needs a paid plan, trial clock, or approval step for production; blocked = cannot build without a partnership/contract or there is no API.
- blocker: the single main blocker ("none" if build_now).
- mcp: official = maintained by the vendor (hosted or repo in vendor org); community = third-party server found; none_found.
- evidence: at least 3 entries (auth, access, api). URLs must be pages you actually opened; quotes must be copied verbatim from those pages, short (<= 25 words).
- confidence: 0-1, your honest probability that access + primary_auth + verdict are all correct.
