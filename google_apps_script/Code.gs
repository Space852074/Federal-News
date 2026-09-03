/**
 * Federal News Google translation proxy.
 *
 * Before deployment, add a Script Property named FEDERAL_NEWS_TOKEN.
 * Deploy as a Web App, execute as yourself, with access granted to anyone.
 */
function doPost(e) {
  try {
    const request = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    const expectedToken = PropertiesService.getScriptProperties()
      .getProperty("FEDERAL_NEWS_TOKEN");
    if (!expectedToken || request.token !== expectedToken) {
      return jsonResponse({ok: false, error: "Unauthorized"});
    }

    const texts = Array.isArray(request.texts) ? request.texts : [];
    if (!texts.length || texts.length > 50) {
      return jsonResponse({ok: false, error: "texts must contain 1 to 50 items"});
    }
    const source = request.source || "en";
    const target = request.target || "zh-CN";
    const translations = texts.map(function(text) {
      return LanguageApp.translate(String(text || ""), source, target);
    });
    return jsonResponse({ok: true, translations: translations});
  } catch (error) {
    return jsonResponse({ok: false, error: String(error)});
  }
}

function jsonResponse(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
