# Bleach Remix — Monitor de Preços e Estoque

Monitor pessoal desenvolvido para acompanhar automaticamente os preços e a disponibilidade dos **26 volumes de Bleach Remix** na loja oficial da Panini Brasil.

O projeto utiliza **Python, GitHub Actions e Telegram**, permitindo realizar o monitoramento sem precisar manter meu computador ligado.

## Monitoramento

O sistema verifica diariamente:

- Preços dos volumes 1 a 26.
- Quedas de preço em relação ao último valor registrado.
- Reposição e esgotamento de estoque.
- Mudanças entre pré-venda e disponibilidade imediata.
- Criação de páginas de volumes ainda não cadastrados na Panini.

As verificações são agendadas para **09h17 (horário de Brasília)**, podendo ocorrer atrasos na execução do GitHub Actions.

## Notificações

O monitor envia alertas diretamente para meu Telegram quando identifica:

| Evento | Notificação |
|---|---|
| Queda de preço | 🔻 |
| Reposição de estoque | ✅ |
| Produto esgotado | ❌ |
| Pré-venda disponível para compra | 📦 |
| Novo volume cadastrado | 🆕 |

Quando não há alterações, nenhuma mensagem é enviada.

## Tecnologias

- **Python** — coleta e processamento dos dados.
- **Requests e BeautifulSoup** — consulta às páginas da Panini.
- **GitHub Actions** — execução automática diária.
- **Telegram Bot API** — recebimento dos alertas.
- **JSON** — armazenamento do histórico de preços e disponibilidade.

## Funcionamento

O monitor consulta as páginas de busca e os produtos individuais, compara os resultados com o histórico armazenado e identifica alterações relevantes.

Os volumes ainda não cadastrados, como os volumes 3 e 16 na verificação inicial, continuam sendo monitorados para detectar quando suas páginas forem disponibilizadas.

O histórico é atualizado automaticamente a cada execução.

## Observações

O monitor é destinado ao meu uso pessoal e não possui vínculo oficial com a Panini.

A disponibilidade das informações depende do funcionamento e da estrutura do site da loja.
