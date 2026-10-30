PICANTE DIRECT OPERATIONS — SEPARATE HOSTED TEST COPY

This package is for a NEW, SEPARATE test service only. Do not replace or redeploy the
existing picante-whatsapp-bot service or repository.

Includes the Flask operations prototype, Gunicorn start command, Render blueprint, and
a basic password gate for hosted testing.

This remains a prototype. It is not connected to WhatsApp, driver-group broadcasts,
driver self-claim UI, payment systems, or GPS. Do not use real customer financial records.

Separate test deployment:
1. Create a NEW GitHub repository (not PicantePizza/picante-whatsapp-bot).
2. Upload the CONTENTS of this folder to the new repository's root.
3. In Render, create a NEW Web Service from that new repository. Do not select the
   existing picante-whatsapp-bot service.
4. Set HOSTED_TEST_MODE=1, TEST_USERNAME and TEST_PASSWORD in the new service's
   environment settings. Set a long random APP_SECRET if it is not generated automatically.
5. Before entering any test records, configure persistent storage and DATABASE_PATH if
   records need to survive service restarts. Otherwise the test database may be lost.
6. Open the new service URL and sign in.

Never put passwords or WhatsApp tokens into GitHub or this ZIP.
