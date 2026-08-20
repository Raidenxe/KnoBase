/* 轻量 i18n: 加载 locale JSON, 提供 t(key) 与 data-i18n 自动翻译。
 * 语言存 localStorage('rag_lang'), 缺省 zh-CN; send() 读取 RAG_LANG 传给后端。 */
(function () {
  'use strict';
  const DEFAULT = 'zh-CN';
  const SUPPORTED = ['zh-CN', 'en-US'];
  let dict = {};
  let lang = localStorage.getItem('rag_lang') || DEFAULT;
  if (SUPPORTED.indexOf(lang) === -1) lang = DEFAULT;

  window.__i18n = {
    lang: lang,
    setLang: function (l) {
      if (SUPPORTED.indexOf(l) === -1) return;
      lang = l;
      localStorage.setItem('rag_lang', l);
      window.RAG_LANG = l.indexOf('en') === 0 ? 'en' : 'zh';
      load();
    },
    t: function (key) {
      return (dict[key] != null ? dict[key] : key);
    }
  };

  function apply() {
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      var k = el.getAttribute('data-i18n');
      if (dict[k] != null) el.textContent = dict[k];
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
      var k = el.getAttribute('data-i18n-placeholder');
      if (dict[k] != null) el.placeholder = dict[k];
    });
    // 语言下拉同步选中态
    var sel = document.getElementById('langSelect');
    if (sel) sel.value = lang;
    document.dispatchEvent(new CustomEvent('i18n:changed', { detail: { lang: lang } }));
  }

  function load() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/locales/' + lang + '.json', true);
    xhr.onload = function () {
      if (xhr.status === 200) {
        try { dict = JSON.parse(xhr.responseText); } catch (e) { dict = {}; }
      }
      window.RAG_LANG = lang.indexOf('en') === 0 ? 'en' : 'zh';
      apply();
    };
    xhr.onerror = function () { window.RAG_LANG = lang.indexOf('en') === 0 ? 'en' : 'zh'; };
    xhr.send();
  }

  window.i18n = window.__i18n;
  window.RAG_LANG = lang.indexOf('en') === 0 ? 'en' : 'zh';
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
})();