-- 0004 — Imóvel com geometria irrecuperável continua na base (sem geometria).
-- Sem isso, a quarentena do imóvel faria a API responder "CAR não encontrado" (IMPEDIDO) para um CAR
-- existente. Com a geometria ausente, R-CAR avalia o status e as regras espaciais saem NAO_VERIFICADO.

ALTER TABLE core_geo.imoveis_car_template
    ALTER COLUMN geometria DROP NOT NULL,
    ADD COLUMN problema_geometria VARCHAR(40),
    ADD CONSTRAINT imoveis_car_geometria_ou_problema CHECK ((geometria IS NULL) = (problema_geometria IS NOT NULL));

-- Seguro apenas enquanto não há versão publicada (a view ainda aponta para o template).
CREATE OR REPLACE VIEW core_geo.imoveis_car_current AS SELECT * FROM core_geo.imoveis_car_template;
