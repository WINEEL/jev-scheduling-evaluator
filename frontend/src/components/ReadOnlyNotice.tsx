/**
 * Said once, on any ministry screen a reader may see and not change.
 *
 * **Why a notice rather than silence.** Task 80 gave an administrator
 * church-wide read access to every ministry and took away every operational
 * write: they can open a team's roles, availability, limits, rules and
 * schedules, and change none of it. Without a line saying so, that screen
 * reads as broken — the controls a head would see are simply missing, with no
 * explanation and nothing to click.
 *
 * **It says whose the ministry is, not what the reader lacks.** "You do not
 * have permission" describes a failure; "this is the ministry head's to run"
 * describes the arrangement, which is what an elder overseeing a church
 * actually wants to know.
 *
 * Rendered from the server's own `can_operate`, never from `is_admin`: an
 * administrator who *also* heads the ministry gets the head's screen, controls
 * and all, and must not see this.
 */

export const READ_ONLY_HEADING = "You are viewing this, not running it";

export function ReadOnlyNotice({
  /** What is on this screen, as a head would name it: "these roles", "this
   *  ministry's availability". Keeps the sentence specific instead of
   *  generic. */
  what,
}: {
  what: string;
}) {
  return (
    <div className="notice notice--info notice--compact" role="status">
      <p className="notice__title">{READ_ONLY_HEADING}</p>
      <p>
        Changing {what} is the ministry head&rsquo;s to do. You can see
        everything here; the controls that change it are theirs.
      </p>
    </div>
  );
}
