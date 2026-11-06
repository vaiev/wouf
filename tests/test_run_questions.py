"""Questions in bot return blocks create events only for substantive items."""

from test_live_state import LiveTest


class RunQuestionsTest(LiveTest):
    def test_empty_forms_create_no_question_event(self):
        forms = ("", "   ", "none", "none.", "no", " . No . ", "n/a", "N/A", "na", "-",
                 "nothing", "no questions", "keine", "...", " . nOnE. ")
        for form in forms:
            with self.subTest(form=form):
                self.block(f"Questions: {form}\n")
                self.run_worker(repo=False)
                self.assertEqual(self.events("question.asked"), [])

    def test_real_question_creates_one_event(self):
        self.block("Questions: Which file?\n")
        self.run_worker(repo=False)
        [event] = self.events("question.asked")
        self.assertEqual(event["data"]["text"], "Which file?")

    def test_mixed_items_keep_only_real_questions(self):
        self.block("Questions: none.\n- Which file?\n- NO\n- n/a.\n- Which branch?\n- keine\nFlags: none\n")
        self.run_worker(repo=False)
        [event] = self.events("question.asked")
        self.assertEqual(event["data"]["text"], "Which file? / Which branch?")
