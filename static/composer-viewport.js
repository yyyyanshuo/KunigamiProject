(function(global) {
  'use strict';
  // Fixed overlays must fit the visible area when a mobile keyboard is open.
  function update() {
    const viewport = global.visualViewport;
    const style = document.documentElement.style;
    style.setProperty('--composer-height', (viewport ? viewport.height : global.innerHeight) + 'px');
    style.setProperty('--composer-top', (viewport ? viewport.offsetTop : 0) + 'px');
    style.setProperty('--composer-left', (viewport ? viewport.offsetLeft : 0) + 'px');
    style.setProperty('--composer-width', (viewport ? viewport.width : global.innerWidth) + 'px');
  }
  global.visualViewport?.addEventListener('resize', update);
  global.visualViewport?.addEventListener('scroll', update);
  global.addEventListener('resize', update);
  update();
})(window);
