# ARM-Hub for Home Assistant

Brings UK heat network meters on ARM-Hub, such as Spot On Energy's, into Home Assistant: heat usage, costs and meter readings.

There are two parts, and you need both:

- **Integration** (`custom_components/armhub`): creates the sensors.
- **Add-on** (`armhub_auth`): handles the ARM-Hub login and token refresh for the integration.

## Install

1. In the add-on store, add this repository and install **UK Heat Network (ARM-Hub) — Auth Addon**.
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
