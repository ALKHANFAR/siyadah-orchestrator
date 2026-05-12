INSERT INTO knowledge_assets (id, project_id, faqs, tone_of_voice, brand_keywords)
VALUES (
    gen_random_uuid()::text,
    'local-dev-project',
    '[{"q":"كم مدة التوصيل؟","a":"3-5 أيام عمل"},{"q":"التوصيل مجاني؟","a":"نعم فوق 200 ريال"},{"q":"سياسة الإرجاع؟","a":"خلال 14 يوم"}]'::jsonb,
    'professional_warm',
    '["فخامة","أصالة","عطور عربية","هدايا VIP"]'::jsonb
);