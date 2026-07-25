# Pending workflow update (one manual step)

`automation.yml.new` is the fixed version of
`.github/workflows/automation.yml`. It could not be pushed automatically:
GitHub blocks apps from modifying workflow files without the `workflows`
permission, so the push was rejected with:

```
refusing to allow a GitHub App to create or update workflow
`.github/workflows/automation.yml` without `workflows` permission
```

## Apply it

```bash
cp .github/workflow-updates/automation.yml.new .github/workflows/automation.yml
rm -rf .github/workflow-updates
git add -A && git commit -m "Apply fixed automation workflow" && git push
```

(Or just paste the file's contents into the GitHub web editor, which is not
subject to the same restriction.)

## Why it matters

| Change | Reason |
|---|---|
| `permissions: contents: write` | **The scheduled run currently fails at the last step.** The default `GITHUB_TOKEN` is read-only, so `git push` of the updated `data/*.json` state 403s — meaning niche picks, the content queue and the upload log were never persisted between runs. |
| `concurrency` group | Two overlapping runs both pop the queue and fight over `data/`, double-publishing or corrupting state. |
| `git pull --rebase` + retry before push | A run that renders for 20 minutes will otherwise fail to push if another run committed meanwhile. |
| `if: always()` on the commit step | A partially refilled queue is preserved instead of being regenerated next run (wasting Groq/YouTube quota). |
| `Run tests` step | Catches dependency drift — exactly how urllib3 2.x silently broke pytrends — before it burns API quota or publishes a broken video. |
| `workflow_dispatch` inputs | Lets you trigger `--dry-run` / `--force-niche` from the Actions tab. |
| `fonts-dejavu-core`, pip cache, 60-min timeout | Smaller/faster install; long-form renders were near the 45-minute limit. |
| Failure artifact upload | Quarantined failed items are downloadable for debugging. |
