package ru.oknovdrugoymir.autoposter;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

public class MainActivity extends Activity {
    private static final String DASHBOARD = "http://158.160.237.113/dashboard";
    private static final String SITE_HOST = "158.160.237.113";
    private static final int FILE_PICKER_REQUEST = 100;

    private WebView browser;
    private android.webkit.ValueCallback<Uri[]> fileCallback;

    private boolean isCabinet(Uri uri) {
        return uri != null && SITE_HOST.equals(uri.getHost()) && "http".equals(uri.getScheme());
    }

    private void openOutside(Uri uri) {
        if (uri == null || (!"https".equals(uri.getScheme()) && !"http".equals(uri.getScheme()))) return;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (android.content.ActivityNotFoundException exception) {
            Toast.makeText(this, "Не удалось открыть ссылку", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        browser = new WebView(this);
        setContentView(browser);
        browser.getSettings().setJavaScriptEnabled(true);
        browser.getSettings().setDomStorageEnabled(true);
        browser.getSettings().setSupportMultipleWindows(true);
        CookieManager.getInstance().setAcceptCookie(true);
        browser.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (isCabinet(request.getUrl())) return false;
                openOutside(request.getUrl());
                return true;
            }
        });
        browser.setDownloadListener((url, userAgent, contentDisposition, mimeType, length) ->
            openOutside(Uri.parse(url)));
        browser.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, android.webkit.ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_PICKER_REQUEST);
                    return true;
                } catch (android.content.ActivityNotFoundException exception) {
                    fileCallback = null;
                    return false;
                }
            }

            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture,
                                          android.os.Message resultMsg) {
                if (!isUserGesture) return false;
                WebView popup = new WebView(MainActivity.this);
                popup.setWebViewClient(new WebViewClient() {
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView popupView, WebResourceRequest request) {
                        openOutside(request.getUrl());
                        popupView.destroy();
                        return true;
                    }
                });
                ((WebView.WebViewTransport) resultMsg.obj).setWebView(popup);
                resultMsg.sendToTarget();
                return true;
            }
        });
        if (state == null) browser.loadUrl(DASHBOARD);
        else browser.restoreState(state);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_PICKER_REQUEST && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            fileCallback = null;
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle state) {
        browser.saveState(state);
        super.onSaveInstanceState(state);
    }

    @Override
    public void onBackPressed() {
        if (browser.canGoBack()) browser.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (fileCallback != null) fileCallback.onReceiveValue(null);
        ((ViewGroup) browser.getParent()).removeView(browser);
        browser.destroy();
        super.onDestroy();
    }
}
