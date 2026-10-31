# Auth Addon

## What it does

ARM-Hub has no simple login for third-party tools. This add-on does the login for the integration:

- **Signs in** using a headless browser, with the email and password you gave the integration.
- **Keeps the login fresh**, renewing it silently and logging in again before it expires.
- **Handles the emailed code.** Logging in again means ARM-Hub emails a 6-digit code, and this add-on can pick that code up by itself if you set up one of the options below.

Your login is stored encrypted in the add-on's data. Nothing is sent anywhere except ARM-Hub, and Google if you use the Gmail option.

## Getting the emailed code

Choose where the add-on reads the code with **Email code source** on its **Configuration** tab: `gmail` (the default), `imap` or `manual`. It only ever uses the one you choose, so setting up a source means selecting it too. With `manual`, or until your chosen source is set up, you'll need to type a code in by hand each time ARM-Hub asks, which is about every 24 hours.

**Use a separate mailbox for your home, not your personal one.** Either automatic option can read that whole mailbox, so a dedicated Gmail account keeps your personal mail out of it. If your ARM-Hub account uses a different address, add a filter there that forwards mail from `arm-hub.com` to the home mailbox. Only email from an `arm-hub.com` address is used.

### Option 1: Gmail (OAuth)

**1. Create the Google project**

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project while signed in to your home Gmail account.
2. Go to **APIs & Services → Library**, find **Gmail API** and enable it.

**2. Set up the consent screen**

1. Go to **Google Auth Platform → Audience** (the consent screen settings). Choose **External**, and fill in the required names and contact email.
2. Add the `.../auth/gmail.readonly` scope under **Data Access**.
3. Under **Audience**, click **Publish app**. Don't leave it in **Testing**: Google ends a test app's access after 7 days, and the code would stop being read. You don't need Google to verify the app for your own use.

**3. Create the client**

1. Go to **Clients → Create client**, and choose **Web application**.
2. Under **Authorized redirect URIs**, add exactly `https://my.home-assistant.io/redirect/oauth`.
3. Copy the **Client ID** and **Client secret**.

**4. Connect it**

1. On this add-on's **Configuration** tab, set **Email code source** to `gmail`, enter the client ID and secret, then restart the add-on.
2. Open the add-on's panel and choose **Gmail auto-MFA settings**.
3. Click **Sign in with Google**. Google warns that the app is unverified, which is expected for your own app: choose **Advanced**, then continue.
4. You land on a `my.home-assistant.io` page. Copy the whole address from the browser's address bar, paste it into the panel and click **Connect**.

### Option 2: IMAP

1. In the home Gmail account, turn on 2-Step Verification and create an [app password](https://myaccount.google.com/apppasswords). Other mail providers work too.
2. In Home Assistant, add the **IMAP** integration with that mailbox.
3. On this add-on's **Configuration** tab, set **Email code source** to `imap`, enter the sensor's entity ID under **IMAP sensor**, then restart the add-on.

### Option 3: By hand

Set **Email code source** to `manual`. When ARM-Hub asks for a code, enter it in this add-on's panel, or in the integration's **MFA Code** entity if that's available.

## Troubleshooting

- **The integration can't reach the add-on:** check the add-on is running, and that the integration's add-on URL is `http://60356650-armhubmeter:8099`.
- **No code is picked up:** check **Email code source** matches what you set up. The add-on's **Log** tab shows each email it looks at, so you can see whether the code arrived and who sent it.
- **Gmail stopped working after about a week:** the Google app was left in **Testing**. Publish it, then click **Reconnect** in the panel.
