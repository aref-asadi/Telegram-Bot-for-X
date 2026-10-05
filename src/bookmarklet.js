/*
 * X -> Telegram | 1-click login bookmarklet
 *
 * Run this while logged in on https://x.com. It:
 *   1. reads auth_token + ct0 from document.cookie,
 *   2. copies a paste-ready fallback payload to the clipboard (always, so a
 *      failed POST can never strand the user),
 *   3. POSTs the cookies to the bot's /auth endpoint for live verification,
 *   4. opens t.me/<bot>?start=auth_<code> with a short one-time handover code.
 *
 * Telegram deep-link constraint: the start parameter is limited to 64
 * characters, which cannot fit auth_token (40 chars) + ct0 (~32 chars). Only
 * the 12-character code travels through Telegram; the cookies stay on the
 * server and are redeemed when the user lands back in the chat.
 *
 * Placeholders below are replaced by the bot when it serves /bookmarklet:
 *   __BASE_URL__     - public https origin of the bot's web server
 *   __BOT_USERNAME__ - bot username without @ ("" disables the deep link)
 *   __CHAT_ID__      - Telegram chat id ("" = unbound code)
 */
(function () {
  var BASE = "__BASE_URL__";
  var BOT = "__BOT_USERNAME__";
  var CHAT = "__CHAT_ID__";

  function readCookie(name) {
    var parts = document.cookie ? document.cookie.split(";") : [];
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i].replace(/^\s+/, "");
      if (p.indexOf(name + "=") === 0) {
        return p.substring(name.length + 1);
      }
    }
    return "";
  }

  var authToken = readCookie("auth_token");
  var ct0 = readCookie("ct0");

  if (!authToken || !ct0) {
    alert("کوکی‌ها پیدا نشدند.\n\nاول وارد x.com شوید و لاگین کنید، بعد دوباره این بوکمارکلت را بزنید.\n\nاگر لاگین هستید و باز هم این پیام را می‌بینید، از روش دستی (F12 → تب Application → Cookies) استفاده کنید.");
    return;
  }

  // Keep a paste-ready copy first - the clipboard fallback must not depend on
  // the network call below.
  try {
    navigator.clipboard.writeText("auth_token=" + authToken + "\nct0=" + ct0);
  } catch (e) {
    // Clipboard may be unavailable; the POST below is the primary path.
  }

  if (!BASE) {
    alert("کوکی‌ها کپی شدند!\n\nآن‌ها را در یک پیام در ربات تلگرام بفرستید.");
    return;
  }

  fetch(BASE + "/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      auth_token: authToken,
      ct0: ct0,
      chat_id: CHAT ? parseInt(CHAT, 10) : null
    })
  })
    .then(function (res) {
      return res
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          return { res: res, data: data };
        });
    })
    .then(function (r) {
      if (r.res.ok && r.data.ok && r.data.code) {
        if (BOT) {
          location.href = "https://t.me/" + BOT + "?start=auth_" + r.data.code;
        } else {
          alert("تأیید شد!\n\nاین کد را در ربات بفرستید:\n\n" + r.data.code);
        }
        return;
      }
      alert("ربات این کوکی‌ها را نپذیرفت.\n\n" + (r.data.error || ("HTTP " + r.res.status)) + "\n\nکوکی‌ها کپی شدند؛ در صورت نیاز آن‌ها را در ربات بفرستید.");
    })
    .catch(function () {
      alert("ارتباط با ربات برقرار نشد.\n\nکوکی‌ها کپی شدند؛ آن‌ها را در یک پیام در ربات بفرستید.");
    });
})();