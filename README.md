# Radar de Ofertas

Monitor de promoções que acompanha uma lista de compras e avisa no Telegram quando aparece uma oferta boa.

- A cada 20 minutos busca ofertas no [Promobit](https://www.promobit.com.br) para os itens pendentes da lista (Google Sheets).
- A busca não usa IA; o Claude só avalia ofertas novas: confere se é o produto certo, compara com o histórico de preços e descarta descontos falsos.
- Cada oferta aprovada chega numa mensagem própria, com print da página, preço, histórico e veredito. Super promoções marcam todo mundo do grupo.

## Configuração

Secrets do repositório: `ANTHROPIC_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `SHEET_CSV_URL` (link de exportação CSV da planilha) e `MENCOES` (`id:Nome,@username`).

A planilha precisa das colunas `Ambiente`, `Item`, `Modelo / Marca`, `Prioridade`, `Comprado`, `Preço máximo (R$)`.
