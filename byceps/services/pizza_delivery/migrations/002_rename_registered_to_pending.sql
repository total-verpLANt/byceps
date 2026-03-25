UPDATE pizza_delivery_entries
   SET status = 'pending'
 WHERE status = 'registered';
