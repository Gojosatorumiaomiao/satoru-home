/* Satoru Home — 轻交互 */
(function () {
  "use strict";

  // 当前页导航高亮
  var here = location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll("nav a[data-nav]").forEach(function (a) {
    if (a.getAttribute("href") === here) a.classList.add("active");
  });

  // 滚动入场
  var items = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.12 });
    items.forEach(function (el) { io.observe(el); });
  } else {
    items.forEach(function (el) { el.classList.add("in"); });
  }

  // 画廊轻提示（不跳转、不请求外网）
  document.querySelectorAll(".art").forEach(function (el) {
    el.addEventListener("click", function () {
      var label = el.querySelector("span");
      if (!label) return;
      var old = label.textContent;
      label.textContent = "占位图 · " + old;
      setTimeout(function () { label.textContent = old; }, 1200);
    });
  });

  // 页脚年份
  var y = document.getElementById("year");
  if (y) y.textContent = new Date().getFullYear();
})();
