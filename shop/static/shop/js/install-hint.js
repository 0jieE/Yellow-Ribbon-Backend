/* On an iPhone in Safari (not yet opened from the Home Screen), explain how
   to install the app. iOS only allows web notifications once it is installed. */
(function () {
    var hint = document.querySelector("[data-install-hint]");
    if (!hint) return;
    var ua = navigator.userAgent;
    var isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    var installed = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
    if (isIOS && !installed) hint.hidden = false;
})();
