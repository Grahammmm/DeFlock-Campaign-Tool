# Fictional shell example

Cedar County is fictional. It has no agencies, allegations, subscribers, map points,
tracking, external fonts or live newsletter endpoint.

Run from the repository root:

```sh
python3 -m campaign_tool build --directory examples/fictional-campaign
python3 -m http.server 8080 --bind 127.0.0.1 --directory examples/fictional-campaign/public
```

Generated public output is ignored by Git. This demonstrates the shared shell and
CSS, not the remaining campaign platform. No accounts or external sends occur.
