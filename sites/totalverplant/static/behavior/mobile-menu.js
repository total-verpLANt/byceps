// Mobile menu toggle
document.addEventListener('DOMContentLoaded', function() {
  const mobileMenuBtn = document.querySelector('.mobile-menu-btn');
  const navLinks = document.querySelector('.nav-links');

  if (mobileMenuBtn && navLinks) {
    mobileMenuBtn.addEventListener('click', function() {
      navLinks.classList.toggle('mobile-menu-open');
      mobileMenuBtn.classList.toggle('active');

      // Update button text
      if (navLinks.classList.contains('mobile-menu-open')) {
        mobileMenuBtn.textContent = '✕';
      } else {
        mobileMenuBtn.textContent = '☰';
      }
    });

    // Close mobile menu when clicking outside
    document.addEventListener('click', function(event) {
      if (!event.target.closest('.nav-links') &&
          !event.target.closest('.mobile-menu-btn') &&
          navLinks.classList.contains('mobile-menu-open')) {
        navLinks.classList.remove('mobile-menu-open');
        mobileMenuBtn.classList.remove('active');
        mobileMenuBtn.textContent = '☰';
      }
    });

    // Close mobile menu when clicking a link
    const navLinksItems = navLinks.querySelectorAll('a');
    navLinksItems.forEach(function(link) {
      link.addEventListener('click', function() {
        navLinks.classList.remove('mobile-menu-open');
        mobileMenuBtn.classList.remove('active');
        mobileMenuBtn.textContent = '☰';
      });
    });
  }
});