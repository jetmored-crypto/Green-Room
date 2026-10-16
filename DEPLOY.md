# Deploying The Green Room — David's click path

Two free accounts, no credit card, ~10 minutes. Everything else is already done.

## Step 1 — Free database (Neon)

1. Go to **neon.tech** and sign up (email or GitHub — free tier, no card).
2. Create a project (name it `green-room`, region closest to you).
3. On the project dashboard, copy the **connection string** — it looks like
   `postgresql://user:password@ep-xxx.us-west-2.aws.neon.tech/neondb?sslmode=require`.
   Keep it handy; you'll paste it in step 2.

## Step 2 — Free web service (Render)

1. Go to **render.com** and sign up with GitHub (free tier, no card).
2. Dashboard → **New** → **Blueprint**.
3. Paste this repo URL: `https://github.com/jetmored-crypto/Green-Room`
4. Render reads `render.yaml` and asks for one value: **DATABASE_URL**.
   Paste the Neon connection string from step 1.
5. Click **Apply**. Render builds and deploys (a few minutes).

Render's free tier sleeps after 15 minutes with nobody talking; the first
message after that takes ~30 seconds to wake it up. Normal from then on.

## Step 3 — Tell me the URL

It'll look like `https://green-room-xxxx.onrender.com`. Send it to me and I will:

1. Verify the deploy is healthy,
2. Mint API keys for Ron and Roman,
3. Write their onboarding notes so they can join.

The operator key (for minting API keys) prints once in the Render logs at
first boot — I only need it once, at key-minting time.
