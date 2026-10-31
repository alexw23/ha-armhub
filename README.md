# ARM-Hub for Home Assistant

Brings UK heat network meters on ARM-Hub, such as Spot On Energy's, into Home Assistant: heat usage, costs and meter readings.

There are two parts, and you need both:

- **Integration** (`custom_components/armhub`): creates the sensors.
- **Add-on** (`armhub_auth`): handles the ARM-Hub login and token refresh for the integration.

## Install

1. In the add-on store, add this repository and install **UK Heat Network (ARM-Hub) — Auth Addon**. Its **Documentation** tab covers [what it does and how to set up the email code](armhub_auth/DOCS.md).
2. In HACS, add this repository as a custom integration repository and download it.
3. Restart Home Assistant, then add **UK Heat Network (ARM-Hub)** under **Settings → Devices & services**.

## API key

ARM-Hub's API needs a key that its portal's web pages already use. It isn't included in this repo, so setup asks for it and explains how to find it:

1. Log in to your heat network's portal in a browser, for example `spot-on-energy.arm-hub.com`.
2. Open the developer tools console, then paste this and press Enter:

   ```js
   (async()=>{for(const s of document.querySelectorAll('script[src*="_next/static/chunks"]')){const m=(await(await fetch(s.src)).text()).match(/Ocp-Apim-Subscription-Key[^,]*?"([0-9a-f]{32})"/);if(m)return console.log(m[1])}console.log('Key not found')})()
   ```

3. Copy the 32-character key it prints into the setup form. It only appears on your screen, and nothing is sent anywhere.

If it says the key wasn't found, open the **Network** tab, reload the page, select a request to `api.armadillo-analytics.com`, and copy its `Ocp-Apim-Subscription-Key` header.

If the integration stops working after the portal changes its key, get the new one the same way and enter it under **Reconfigure**.

## Compatibility

Tested with [Spot On Energy](https://spot-on-energy.arm-hub.com/)'s portal. Other heat networks on ARM-Hub may work but are untested, because requests currently identify themselves as coming from Spot On Energy's portal.

This is an independent project, not affiliated with or endorsed by Spot On Energy, ARM-Hub, Armadillo Analytics or any heat network operator.

## Development

**Changes:** add them under `[Unreleased]` in `CHANGELOG.md`. Tests run on every pull request, along with a build of the add-on image.

**Trying what's on `main`:** every push to `main` publishes a `dev` image. To test it, install **Auth Addon (dev)**, stop the normal add-on, and set the integration's add-on URL to `http://60356650-armhubmeter-dev:8099`. The dev add-on keeps its own data, so it logs in afresh.

**Releasing:** publish a GitHub release tagged like `v2.3.0`. That builds the images, then sets the version in the add-on and integration and stamps the changelog on `main`. The add-on store only sees a new version once its image exists, which is why the bump comes last.
