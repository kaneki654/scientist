package com.scientist.tether;

import android.app.Activity;
import android.content.Intent;
import android.net.VpnService;
import android.os.Bundle;

/**
 * Invisible entry point used by the PC.
 *   START: asks for VPN consent if needed (the PC normally pre-grants it with
 *          "appops set com.scientist.tether ACTIVATE_VPN allow", so no dialog appears), then starts the service.
 *   STOP:  stops the service.
 * Extras: "dns" (comma separated, default 8.8.8.8).
 */
public class StartActivity extends Activity {
    private static final int REQUEST_VPN = 1;
    private String dns;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent intent = getIntent();
        String action = intent == null ? null : intent.getAction();
        if (TetherService.ACTION_STOP.equals(action)) {
            startService(new Intent(this, TetherService.class).setAction(TetherService.ACTION_STOP));
            finish();
            return;
        }
        dns = intent == null ? null : intent.getStringExtra("dns");
        Intent consent = VpnService.prepare(this);
        if (consent == null) {
            startTether();
            finish();
        } else {
            startActivityForResult(consent, REQUEST_VPN);
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQUEST_VPN && resultCode == RESULT_OK) {
            startTether();
        }
        finish();
    }

    private void startTether() {
        Intent i = new Intent(this, TetherService.class).setAction(TetherService.ACTION_START);
        if (dns != null) {
            i.putExtra("dns", dns);
        }
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            startForegroundService(i);
        } else {
            startService(i);
        }
    }
}
