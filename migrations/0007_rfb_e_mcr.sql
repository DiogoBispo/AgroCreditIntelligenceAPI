-- 0007 — RFB completa e atributos do imóvel exigidos pelo texto do MCR 2-9 (atualização 756).

-- Minimização: a carga da RFB traz só CNPJ, situação, CNAE e município; a razão social não é usada.
ALTER TABLE core_geo.empresas_template ALTER COLUMN razao_social DROP NOT NULL;

-- MCR 2-9 item 13 (CAR de uso coletivo: assentamento, povos tradicionais) e itens 15 e 17 (prazos e
-- exceções por módulos fiscais). Valores vêm do SICAR (tipo_imovel: IRU, AST, PCT; m_fiscal).
ALTER TABLE core_geo.imoveis_car_template
    ADD COLUMN tipo_imovel VARCHAR(5),
    ADD COLUMN modulos_fiscais NUMERIC(10, 4);
