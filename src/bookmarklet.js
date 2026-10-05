/*
1-click browser Bookmarklet for Telegram Bot Authentication

This bookmarklet extracts Twitter/X credentials (auth_token, ct0) from the
browser's document.cookie and securely sends them to the bot's auth endpoint.

Installation:
1. Drag the bookmarklet to your browser's bookmarks bar
2. Visit https://x.com while logged into your Twitter account
3. Click the bookmarklet - credentials are sent to your Telegram bot

Production URL:
https://your-domain.com/bookmarklet.html
*/

(function(){
    'use strict';
    
    // Click handler - extract tokens and send to bot
    async function main() {
        try {
            const cookies = document.cookie.split(';');
            const auth = cookies.find(c => c.trim().startsWith('auth_token='));
            const ct0 = cookies.find(c => c.trim().startsWith('ct0='));
            
            if (!auth || !ct0) {
                alert('Please visit https://x.com while logged in, then try again.');
                return;
            }
            
            const authToken = auth.split('=')[1];
            const ct0Val = ct0.split('=')[1];
            
            alert('Extracting credentials...\n\nLoading bot...');
            
            // Send POST to bot's auth endpoint
            const botEndpoint = 'YOUR_BOT_AUTH_ENDPOINT';
            
            if (botEndpoint && botEndpoint !== 'YOUR_BOT_AUTH_ENDPOINT') {
                try {
                    const payload = JSON.stringify({
                        auth_token: authToken,
                        ct0: ct0Val
                    });
                    
                    const response = await fetch(botEndpoint, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: payload
                    });
                    
                    if (response.ok) {
                        const data = await response.json();
                        alert('✅ ' + (data.message || 'Credentials validated successfully!'));
                        return;
                    }
                } catch (e) {
                    console.log('Bot endpoint not reachable:', e);
                }
            }
            
            // Fallback: copy to clipboard
            const text = 'auth_token=' + authToken + '\nct0=' + ct0Val;
            await navigator.clipboard.writeText(text);
            alert('Credentials copied to clipboard!\n\nPaste them using /auth command in Telegram.');
            
        } catch (e) {
            console.error('Bookmarklet error:', e);
            alert('Error: ' + e.message);
        }
    }
    
    main();
})();