use crate::{Error, Result};
use std::cell::{Cell, RefCell};
use std::future::{poll_fn, Future};
use std::pin::pin;
use std::rc::Rc;
use std::task::{Poll, Waker};

#[derive(Default)]
struct State {
    cancelled: Cell<bool>,
    wakers: RefCell<Vec<Waker>>,
}

/// Stops a running turn. `Clone` shares the same switch, and one instance serves one turn.
#[derive(Clone, Default)]
pub struct Cancel(Rc<State>);

impl Cancel {
    pub fn cancel(&self) {
        self.0.cancelled.set(true);
        for waker in self.0.wakers.borrow_mut().drain(..) {
            waker.wake();
        }
    }

    pub fn is_cancelled(&self) -> bool {
        self.0.cancelled.get()
    }

    /// Runs `future` until it ends or this switch fires. Cancelling drops the future,
    /// and dropping an in-flight browser request aborts it.
    pub async fn run<T>(&self, future: impl Future<Output = T>) -> Result<T> {
        let mut future = pin!(future);
        poll_fn(|cx| {
            if self.is_cancelled() {
                return Poll::Ready(Err(Error::Cancelled));
            }
            let mut wakers = self.0.wakers.borrow_mut();
            if !wakers.iter().any(|w| w.will_wake(cx.waker())) {
                wakers.push(cx.waker().clone());
            }
            drop(wakers);
            future.as_mut().poll(cx).map(Ok)
        })
        .await
    }
}
