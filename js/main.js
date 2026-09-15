/* Satoru Home — 轻交互 */
(function () {
  "use strict";

  // ---------- 入场动画的启用与兜底（必须最先做） ----------
  // 先取好元素、装好恢复逻辑，确认兜底就位后才加上 .js 标记。
  // 这样无论后续哪一行抛异常，正文都不会停在隐藏状态。
  var revealItems = document.querySelectorAll(".reveal");

  function revealAll() {
    for (var i = 0; i < revealItems.length; i++) {
      revealItems[i].classList.add("in");
    }
  }

  function enableRevealAnimation() {
    try {
      var reduceMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reduceMotion || !("IntersectionObserver" in window)) {
        revealAll();
        return;
      }
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        });
      }, { threshold: 0.12 });
      for (var j = 0; j < revealItems.length; j++) {
        io.observe(revealItems[j]);
      }
      // 兜底：观察器若未触发，3 秒后仍显示内容
      setTimeout(revealAll, 3000);
    } catch (err) {
      // 初始化失败也不能让正文消失
      revealAll();
    }
  }

  // 标记 + 兜底绑在一起：先装上“无论如何都恢复可见”的安全网，
  // 再启用需要隐藏才能生效的动画。
  window.addEventListener("error", revealAll);
  try {
    document.documentElement.classList.add("js");
    enableRevealAnimation();
  } catch (err) {
    document.documentElement.classList.remove("js");
    revealAll();
  }

  // 当前页导航高亮
  var here = location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll("nav a[data-nav]").forEach(function (a) {
    if (a.getAttribute("href") === here) {
      a.classList.add("active");
      a.setAttribute("aria-current", "page");
    }
  });

  // 移动端导航开关
  var toggle = document.querySelector(".nav-toggle");
  var links = document.getElementById("nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", function () {
      var open = links.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "收起导航菜单" : "展开导航菜单");
    });

    // 跳转后自动收起，避免返回时菜单残留展开
    links.addEventListener("click", function (e) {
      if (e.target.closest("a") && links.classList.contains("open")) {
        links.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-label", "展开导航菜单");
      }
    });
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
