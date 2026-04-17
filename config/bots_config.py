bots = {}


def get_all_bots():
    from state.state_manager import state_manager
    return state_manager.get_all_bots()


default_limit = 100

messages = [
"India-only chat\nSearch: allindiachatbot",
"Chat with real Indian users\nSearch: allindiachatbot",
"Talk only with Indians\nSearch: allindiachatbot",
"No foreign users, only Indians\nSearch: allindiachatbot",
"Better chats with Indian users\nSearch: allindiachatbot",
"Find Indian people to chat with\nSearch: allindiachatbot",
"Skip random chats, try Indian chat\nSearch: allindiachatbot",
"Only Indian users available\nSearch: allindiachatbot",
"Talk with Indians only\nSearch: allindiachatbot",
"Real Indian chats\nSearch: allindiachatbot",

"Looking for Indian chat?\nSearch: allindiachatbot",
"Indian chat only platform\nSearch: allindiachatbot",
"Connect with Indians\nSearch: allindiachatbot",
"Better than random global chat\nSearch: allindiachatbot",
"Focused Indian chat experience\nSearch: allindiachatbot",
"Find Indian strangers\nSearch: allindiachatbot",
"Only Indian connections\nSearch: allindiachatbot",
"No random international chats\nSearch: allindiachatbot",
"Talk with people from India\nSearch: allindiachatbot",
"Indian anonymous chat\nSearch: allindiachatbot",

"Simple Indian chat\nSearch: allindiachatbot",
"Indian-only platform\nSearch: allindiachatbot",
"Meet Indians online\nSearch: allindiachatbot",
"Chat only with Indians\nSearch: allindiachatbot",
"Indian users only\nSearch: allindiachatbot",
"Better Indian chat option\nSearch: allindiachatbot",
"Indian chat experience\nSearch: allindiachatbot",
"Only India-based users\nSearch: allindiachatbot",
"Connect with Indian audience\nSearch: allindiachatbot",
"Indian-only conversations\nSearch: allindiachatbot",

"Talk to Indians without filters\nSearch: allindiachatbot",
"Indian chat network\nSearch: allindiachatbot",
"Find Indian friends online\nSearch: allindiachatbot",
"Only India chat system\nSearch: allindiachatbot",
"Chat with Indian strangers\nSearch: allindiachatbot",
"Indian chat platform\nSearch: allindiachatbot",
"Indian user base chat\nSearch: allindiachatbot",
"Try Indian-only chat\nSearch: allindiachatbot",
"Chat with Indians easily\nSearch: allindiachatbot",
"Indian chat room\nSearch: allindiachatbot",

"Only Indian connections here\nSearch: allindiachatbot",
"Switch to Indian chat\nSearch: allindiachatbot",
"Focused Indian chat\nSearch: allindiachatbot",
"Indian users waiting\nSearch: allindiachatbot",
"Better than mixed chats\nSearch: allindiachatbot",
"Indian chat service\nSearch: allindiachatbot",
"Chat with Indian people\nSearch: allindiachatbot",
"Indian chat only system\nSearch: allindiachatbot",
"Talk with Indians instantly\nSearch: allindiachatbot",
"Indian chat hub\nSearch: allindiachatbot",

"Meet Indian users\nSearch: allindiachatbot",
"Indian chat community\nSearch: allindiachatbot",
"Only Indians chatting\nSearch: allindiachatbot",
"Chat India-only\nSearch: allindiachatbot",
"Indian chat connection\nSearch: allindiachatbot",
"Find Indian matches\nSearch: allindiachatbot",
"Indian chat service online\nSearch: allindiachatbot",
"Only India users chat\nSearch: allindiachatbot",
"Indian chat alternative\nSearch: allindiachatbot",
"Try India-only system\nSearch: allindiachatbot",

"Indian users online\nSearch: allindiachatbot",
"Connect with Indian strangers\nSearch: allindiachatbot",
"Indian-only conversations here\nSearch: allindiachatbot",
"Better Indian chat system\nSearch: allindiachatbot",
"Only Indian chat option\nSearch: allindiachatbot",
"India-focused chat\nSearch: allindiachatbot",
"Find Indians to chat\nSearch: allindiachatbot",
"Indian chat service available\nSearch: allindiachatbot",
"Indian user chats only\nSearch: allindiachatbot",
"Talk with Indian community\nSearch: allindiachatbot",

"Indian-only anonymous chat\nSearch: allindiachatbot",
"Only India connections\nSearch: allindiachatbot",
"Indian chat users active\nSearch: allindiachatbot",
"Chat with India-based users\nSearch: allindiachatbot",
"Indian chat network online\nSearch: allindiachatbot",
"Better chats with Indians\nSearch: allindiachatbot",
"Only Indian audience\nSearch: allindiachatbot",
"Indian chat alternative platform\nSearch: allindiachatbot",
"Connect with Indian chat users\nSearch: allindiachatbot",
"Indian chat system online\nSearch: allindiachatbot"
]