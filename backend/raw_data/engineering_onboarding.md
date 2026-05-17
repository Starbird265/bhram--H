# Engineering Onboarding Guide

## Code Review Process
All pull requests must have at least two approving reviews before merging.
Do not bypass branch protection rules under any circumstances.

## Deployment Steps
1. Ensure all tests pass in CI.
2. Tag the release in GitHub.
3. Deploy to staging via Vercel dashboard.
4. Wait for QA sign-off.
5. Promote to production.
