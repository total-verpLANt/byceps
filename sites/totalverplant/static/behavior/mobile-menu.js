document.addEventListener('DOMContentLoaded', function() {
  const headerNav = document.querySelector('.header-nav');
  const container = headerNav ? headerNav.querySelector('.container') : null;
  const logoContainer = headerNav
    ? headerNav.querySelector('.logo-container')
    : null;
  const navLinks = headerNav ? headerNav.querySelector('.nav-links') : null;
  const mobileMenuBtn = headerNav
    ? headerNav.querySelector('.mobile-menu-btn')
    : null;
  const headerUserNav = headerNav
    ? headerNav.querySelector('.header-user-nav')
    : null;

  if (
    !headerNav ||
    !container ||
    !logoContainer ||
    !navLinks ||
    !mobileMenuBtn ||
    !headerUserNav
  ) {
    return;
  }

  let frameRequest = null;
  const collapseBufferPx = 8;

  function setMenuButtonIcon(isOpen) {
    const buttonText = mobileMenuBtn.querySelector('[aria-hidden]');
    if (buttonText) {
      buttonText.textContent = isOpen ? '✕' : '☰';
    }
  }

  function closeMenu() {
    navLinks.classList.remove('mobile-menu-open');
    mobileMenuBtn.classList.remove('active');
    mobileMenuBtn.setAttribute('aria-expanded', 'false');
    setMenuButtonIcon(false);
  }

  function openMenu() {
    navLinks.classList.add('mobile-menu-open');
    mobileMenuBtn.classList.add('active');
    mobileMenuBtn.setAttribute('aria-expanded', 'true');
    setMenuButtonIcon(true);
  }

  function toggleMenu() {
    if (navLinks.classList.contains('mobile-menu-open')) {
      closeMenu();
    } else {
      openMenu();
    }
  }

  function setCollapsedState(isCollapsed) {
    const wasCollapsed = headerNav.classList.contains('is-collapsed');
    if (wasCollapsed === isCollapsed) {
      return;
    }

    headerNav.classList.toggle('is-collapsed', isCollapsed);
    closeMenu();
  }

  function isCssMobileLayout() {
    return window.matchMedia('(max-width: 950px)').matches;
  }

  function evaluateLayout() {
    if (frameRequest !== null) {
      window.cancelAnimationFrame(frameRequest);
    }

    frameRequest = window.requestAnimationFrame(function() {
      frameRequest = null;

      if (isCssMobileLayout()) {
        if (headerNav.classList.contains('is-collapsed')) {
          headerNav.classList.remove('is-collapsed');
          closeMenu();
        }
        return;
      }

      if (headerNav.classList.contains('is-collapsed')) {
        // Temporarily un-collapse to measure real expanded layout
        headerNav.classList.remove('is-collapsed');
        var wouldOverflow = container.scrollWidth > container.clientWidth + collapseBufferPx;
        headerNav.classList.add('is-collapsed');

        if (!wouldOverflow) {
          setCollapsedState(false);
        }
      } else {
        // When expanded, check for overflow directly — most reliable signal
        var overflows = container.scrollWidth > container.clientWidth + collapseBufferPx;
        if (overflows) {
          setCollapsedState(true);
        }
      }
    });
  }

  mobileMenuBtn.addEventListener('click', function() {
    toggleMenu();
  });

  document.addEventListener('click', function(event) {
    if (
      !event.target.closest('.nav-links') &&
      !event.target.closest('.mobile-menu-btn') &&
      navLinks.classList.contains('mobile-menu-open')
    ) {
      closeMenu();
    }
  });

  navLinks.querySelectorAll('a').forEach(function(link) {
    link.addEventListener('click', function() {
      closeMenu();
    });
  });

  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape' && navLinks.classList.contains('mobile-menu-open')) {
      closeMenu();
      mobileMenuBtn.focus();
    }
  });

  window.addEventListener('resize', evaluateLayout);

  if (typeof ResizeObserver !== 'undefined') {
    var resizeObserver = new ResizeObserver(function() {
      evaluateLayout();
    });

    resizeObserver.observe(container);
    resizeObserver.observe(logoContainer);
    resizeObserver.observe(headerUserNav);
  }

  if (typeof MutationObserver !== 'undefined') {
    var mutationObserver = new MutationObserver(function() {
      evaluateLayout();
    });

    mutationObserver.observe(navLinks, {
      childList: true,
      subtree: true,
      characterData: true,
    });

    mutationObserver.observe(headerUserNav, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready
      .then(function() {
        evaluateLayout();
      })
      .catch(function() {
        evaluateLayout();
      });
  }

  window.addEventListener('load', function() {
    // Double-rAF ensures layout is fully recalculated after font application
    requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        evaluateLayout();
      });
    });
  });

  closeMenu();
  evaluateLayout();
});
